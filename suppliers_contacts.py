import os
import json
from io import BytesIO
from time import sleep
from typing import Tuple, List, Dict, Any, Optional
from PIL import Image

from difflib import SequenceMatcher
import base64
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import html2text

from tqdm import tqdm

from openai import OpenAI



import re
import helium
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from helium import Link

driver = None


def get_driver() -> webdriver.Chrome:
    global driver
    if driver is None:
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--force-device-scale-factor=1")
        chrome_options.add_argument("--window-size=1350,1000")
        chrome_options.add_argument("--disable-pdf-viewer")
        chrome_options.add_argument("--window-position=0,0")
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        driver = helium.start_chrome(headless=True, options=chrome_options)
        driver.set_page_load_timeout(int(os.environ.get("PAGE_LOAD_TIMEOUT", "20")))
    return driver


def shutdown_driver() -> None:
    global driver
    if driver:
        try:
            driver.close()
        finally:
            driver = None


client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ.get("OPENAI_BASE_URL"))


SUMMARY_INSTRUCTIONS = """Твоя задача — преобразовать техническое задание с длинным перечнем однотипных товаров
в компактное, но информативное ТЗ для поиска ОДНОГО поставщика, который сможет поставить весь перечень.

Важно:
- Не нужно перечислять все позиции поштучно.
- Нужно сохранить информацию о специфике ассортимента и требований, но более агрегированно.

СХЕМА ОТВЕТА (JSON):

{
  "item": "обобщённое наименование закупки в 1–2 строках (например: 'Поставка автошин для легкового, грузового и специального транспорта ...')",
  "product_groups": [
    {
      "group_name": "краткое название группы ",
      "short_description": "кракое описание спицифики, диапазонов характеристик (2–3 предложения)",
    }
  ],

  "search_queries": [
    "список реалистичных поисковых запросов для Яндекс-поиска поставщиков/дистрибьюторов/производителей,
     2-3 запроса на РУССКОМ, с ключевыми словами вида:
     'поставщик', 'опт', 'официальный дилер', 'производитель', 'каталог', 'дистрибьютор', 'купить'"
  ]
}

Правила:
- Не выдумывай технических характеристик, используй только то, что логично следует из исходного ТЗ.
- Пиши коротко, но сохрани разнообразие ассортимента: выдели отдельные группы если они есть, иначе сделай одну группу.
- Все формулировки — на русском языке.
- Верни ТОЛЬКО JSON по указанной схеме, без пояснений и комментариев
- Отсортируй "search_queries" от самого релевантного
"""

DOC_VAL_INSTRUCTIONS = """Вы — помощник по поиску прямых поставщиков (производителей или официальных дистрибьюторов)
для следующей закупки.

ТЕХНИЧЕСКОЕ ЗАДАНИЕ (сводка):
{technical_spec}

ВАША ЗАДАЧА:
Оценить, является ли данный поисковый результат потенциально релевантным поставщиком
для ЭТОЙ закупки.

ПОИСКОВЫЙ РЕЗУЛЬТАТ:
{link}
**{title}**
{text}

КРИТЕРИИ РЕЛЕВАНТНОСТИ:
- "Релевантно", если по тексту видно, что сайт относится к:
  - производителю, заводу, фабрике;
  - официальному дилеру / дистрибьютору;
  - оптовому поставщику/дистрибьютору нужного ассортимента.
- "НЕ релевантно", если:
  - это маркетплейсы и агрегаторы (Ozon, Wildberries, Яндекс.Маркет, Aliexpress, Alibaba, Amazon и т.п.);
  - доски объявлений, каталоги-агрегаторы, сервисы объявлений;
  - блоги, статьи, справочники, энциклопедии;
  - сайт явно про другую сферу.

ФОРМАТ ОТВЕТА — ТОЛЬКО JSON:
{{
  "is_relevant": true/false,
  "reason": "краткое объяснение на русском"
}}
"""

COMPANY_VAL_INSTRUCTIONS = """Ты — эксперт по закупкам.
Согласно техническому заданию тебе нужно закупить:
{tz}

Тебе нужно определить, работает ли компания в нужной сфере/отрасли.
ДОСТУПНЫЕ ДАННЫЕ САЙТА (текст, полученный с разных страниц):

{site_text_block}

ТВОЯ ЗАДАЧА:
На основе ТЗ и текстов сайта решить, подходит ли компания как поставщик для этой закупки.

КРИТЕРИИ (НЕ БУДЬ ПРИДИРЧИВ):
- "Да" (релевантна), если:
  * компания производит или поставляет товары, сходные с перечнем из ТЗ;
  * или явно указаны оптовые поставки/дистрибьюция нужной номенклатуры;
- "Нет", если:
  * другая отрасль/сфера (например, только бытовые лампочки вместо промышленного освещения);
  * или из текста видно, что это не производитель/поставщик нужного типа товара.

Требуется также по возможности вытащить название компании с сайта.

ФОРМАТ ОТВЕТА — СТРОГО JSON (без комментариев, без пояснений вокруг):
{{
  "is_relevant": true/false,
  "reason": "краткое объяснение на русском, почему да/нет",
  "name": "название компании, если удалось определить, иначе null",
}}
"""

FIX_JSON_INSTRUCTIONS = """
You are a data transformation assistant. Your task is to process the given answer and convert it to valid JSON format according to the task specification.

TASK DESCRIPTION:
{task}

RECEIVED ANSWER:
{received_answer}

INSTRUCTIONS:
1. Carefully analyze the task requirements and the received answer
2. Extract all relevant information from the answer
3. Transform it into valid JSON that matches the schema specified in the task
4. If the received answer is incomplete or ambiguous, make reasonable assumptions based on the task context
5. Ensure the JSON is properly structured and valid
6. Do not add any explanatory text - output only the JSON

OUTPUT REQUIREMENTS:
- Output must be valid JSON
- You may output either pure JSON or a JSON code block (```json ... ```)
- The JSON must strictly follow the schema described in the task

Important: Your output should contain ONLY the JSON data, no additional text.
"""


def summarize_tz_for_single_supplier(tz_text: str) -> Dict[str, Any]:
    """
    Преобразует большое ТЗ с длинным перечнем однотипных товаров в сводку
    для поиска ОДНОГО поставщика, который может закрыть весь перечень.

    Возвращает JSON со схемой:
    {
      "item": str,             # обобщённое наименование закупки (1–2 строки)
      "summary_spec": str,     # сжатое, но информативное описание требований
      "product_groups": [
        {
          "group_name": str,
          "short_description": str,
        }
      ],
      "search_queries": [str]  # готовые запросы для Яндекс-поиска поставщиков
    }
    """

    prompt = f"{SUMMARY_INSTRUCTIONS}\n\nИсходное техническое задание:\n{tz_text}"

    response = client.chat.completions.create(
        model=os.environ["OPENAI_MODEL"],
        messages=[
            {
                "role": "system",
                "content": "Ты помощник по структурированию технических заданий для поиска одного поставщика."
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=2500,
    )

    raw_text = response.choices[0].message.content.strip()
    parsed = parse_json_response(raw_text)

    # Минимальная защита от поломок
    result: Dict[str, Any] = {
        "item": (parsed.get("item") or "").strip(),
        "product_groups": parsed.get("product_groups") or [],
        "search_queries": [q.strip() for q in parsed.get("search_queries") or [] if isinstance(q, str) and q.strip()],
    }
    return result



def build_validation_tz(tz_summary: Dict[str, Any]) -> str:
    """
    Строит компактное текстовое ТЗ для задачи валидации сайта:
    используется browser_agent, чтобы понять: релевантен поставщик или нет.

    tz_summary — это результат summarize_tz_for_single_supplier(...):
      {
        "item": str,
        "product_groups": [...],
        "search_queries": [...]
      }
    """
    item = (tz_summary.get("item") or "").strip()
    summary_spec = (tz_summary.get("summary_spec") or "").strip()
    product_groups = tz_summary.get("product_groups") or []

    lines = []

    if item:
        lines.append(f"Наименование закупки: {item}")

    if summary_spec:
        lines.append("\nКраткое описание и требования:")
        lines.append(summary_spec)

    if product_groups:
        lines.append("\nОсновные группы продукции :")
        for g in product_groups:
            name = (g.get("group_name") or "").strip()
            desc = (g.get("short_description") or "").strip()

            group_lines = []
            if name:
                group_lines.append(f"- Группа: {name}")
            if desc:
                group_lines.append(f"  Описание: {desc}")

            if group_lines:
                lines.append("\n".join(group_lines))

    return "\n".join(lines)


def transform_answer_to_json(task: str, received_answer: str) -> Dict[str, Any]:
    """
    Transform a received answer into JSON format according to task specification.
    
    Args:
        task: Description of the task and JSON schema
        received_answer: The answer to transform
        
    Returns:
        Parsed JSON as dictionary
    """
    prompt = FIX_JSON_INSTRUCTIONS(task=task, received_answer=received_answer)
    try:
        response = client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=[
                {"role": "system", "content": "You are a JSON transformation specialist."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2000
        )
        
        response_text = response.choices[0].message.content.strip()
        return parse_json_response(response_text)
        
    except Exception as e:
        raise Exception(f"Failed to transform answer to JSON: {str(e)}")


def parse_json_response(response_text: str) -> Dict[str, Any]:
    """
    Parse JSON response, handling both pure JSON and JSON code blocks.
    
    Args:
        response_text: Response text that may contain JSON
        
    Returns:
        Parsed JSON as dictionary
    """
    if not response_text:
        raise ValueError("Empty response received")
    
    # Clean the response text
    cleaned_text = response_text.strip()
    
    # Try to extract JSON from code block
    json_block_pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
    match = re.search(json_block_pattern, cleaned_text)
    
    if match:
        json_str = match.group(1).strip()
    else:
        # If no code block, use the entire text
        json_str = cleaned_text
    
    # Remove any non-JSON text before or after the JSON
    # Look for the first { or [ and last } or ]
    start_chars = {'{': '}', '[': ']'}
    
    # Find first occurrence of { or [
    start_pos = -1
    start_char = None
    for char in ['{', '[']:
        pos = json_str.find(char)
        if pos != -1 and (start_pos == -1 or pos < start_pos):
            start_pos = pos
            start_char = char
    
    if start_pos != -1:
        # Find matching closing bracket
        end_char = start_chars[start_char]
        stack = []
        end_pos = -1
        
        for i in range(start_pos, len(json_str)):
            char = json_str[i]
            if char == start_char:
                stack.append(char)
            elif char == end_char:
                stack.pop()
                if not stack:
                    end_pos = i
                    break
        
        if end_pos != -1:
            json_str = json_str[start_pos:end_pos + 1]
        else:
            # If no matching end bracket, take from start to end
            json_str = json_str[start_pos:]
    
    # Parse JSON
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # Try to fix common JSON issues
        fixed_json = fix_common_json_issues(json_str)
        try:
            return json.loads(fixed_json)
        except json.JSONDecodeError:
            raise ValueError(f"Failed to parse JSON. Error: {str(e)}\nText: {json_str[:500]}")


def fix_common_json_issues(json_str: str) -> str:
    """Attempt to fix common JSON formatting issues."""
    # Remove trailing commas
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    
    # Fix single quotes to double quotes (carefully)
    # This is a simple approach - for complex cases, a proper parser would be needed
    lines = json_str.split('\n')
    fixed_lines = []
    
    for line in lines:
        # Only replace single quotes that appear to be around keys or string values
        in_string = False
        chars = list(line)
        i = 0
        
        while i < len(chars):
            if chars[i] == '"' and (i == 0 or chars[i-1] != '\\'):
                in_string = not in_string
            elif chars[i] == "'" and not in_string:
                # Check if this single quote is likely a string delimiter
                # Look for pattern: whitespace or : before, and : or whitespace after
                prev_char = chars[i-1] if i > 0 else ' '
                next_char = chars[i+1] if i < len(chars)-1 else ' '
                
                if prev_char.isspace() or prev_char in '{[,:' or next_char.isspace() or next_char in '},:':
                    chars[i] = '"'
            i += 1
        
        fixed_lines.append(''.join(chars))
    
    return '\n'.join(fixed_lines)


def fuzzy_matched(query: str, candidate: str, min_ratio: float = 0.6) -> bool:
    """
    Fuzzy compare two texts.
    Returns True if:
      - query is a substring of candidate (or vice versa), or
      - similarity ratio >= min_ratio.
    """
    q = query.strip().lower()
    c = candidate.strip().lower()
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    return SequenceMatcher(None, q, c).ratio() >= min_ratio


# Set up screenshot callback
def get_screenshot() -> None:
    drv = get_driver()
    png_bytes = drv.get_screenshot_as_png()
    image = Image.open(BytesIO(png_bytes))
    return image


def search_item_ctrl_f(text: str, nth_result: int = 1) -> str:
    """
    Searches for text on the current page via Ctrl + F and jumps to the nth occurrence.
    Args:
        text: The text to search for
        nth_result: Which occurrence to jump to (default: 1)
    """
    drv = get_driver()
    elements = drv.find_elements(By.XPATH, f"//*[contains(text(), '{text}')]")
    if nth_result > len(elements):
        raise Exception(f"Match n°{nth_result} not found (only {len(elements)} matches found)")
    result = f"Found {len(elements)} matches for '{text}'."
    elem = elements[nth_result - 1]
    drv.execute_script("arguments[0].scrollIntoView(true);", elem)
    result += f"Focused on element {nth_result} of {len(elements)}"
    return result


def go_back() -> None:
    """Goes back to previous page."""
    get_driver().back()


def close_popups() -> str:
    """
    Closes any visible modal or pop-up on the page. Use this to dismiss pop-up windows!
    This does not work on cookie consent banners.
    """
    webdriver.ActionChains(get_driver()).send_keys(Keys.ESCAPE).perform()


def visit_website(url: str) -> str:
    """
    Open a webpage in browser.
    Args:
        url: The page to load.
    Returns:
        Confirmation string.
    """
    get_driver()
    helium.go_to(url)
    return f"Opened {url}"


def find_links(text: str) -> List[Link]:
    """
    Find Link elements by fuzzy text.
    Args:
        text: Visible link text (approximate).
    Returns:
        List of Link elements.
    """
    get_driver()
    all_links = helium.find_all(Link())
    results: List[helium.Link] = []
    for link in all_links:
        try:
            link_text = link.web_element.text.strip()
            if fuzzy_matched(text, link_text):
                results.append(link)
        except NoSuchElementException:
            pass

    return results


def click_link(element: helium.Link) -> str:
    """
    Click a browser element object.
    Args:
        element: Element returned by find_links(...).
    Returns:
        Confirmation string.
    """
    get_driver()
    helium.click(element)
    return "Element clicked"


def scroll_page(direction: str = "down", num_pixels: int = 1200) -> str:
    """
    Scroll page up/down.
    Args:
        direction: "down" or "up".
        num_pixels: Pixels to scroll.
    Returns:
        Confirmation string.
    """
    get_driver()
    helium.scroll_down(num_pixels) if direction == "down" else helium.scroll_up(num_pixels)
    return f"Scrolled {direction} {num_pixels}px"


def get_emails() -> List[str]:
    """
    Extract email addresses from current page HTML.
    Returns:
        List of unique emails.
    """
    html = get_driver().page_source or ""
    # Simple RFC-like email regex
    pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    emails = re.findall(pattern, html)
    # Deduplicate while preserving order
    seen = set()
    result: List[str] = []
    for e in emails:
        if e not in seen:
            seen.add(e)
            result.append(e)

    return result


def open_about_section() -> bool:
    """
    Try to find on current page and open "About" section
    Returns:
        Confirmation bool. Does section "About" was found and opend successfully.
    """
    contacts_texts = ["о нас", "о компании", "о заводе", "про нас", "о бренде"]

    for link_text in contacts_texts:
        links = find_links(link_text)
        if links:
            try:
                click_link(links[0])
            except Exception as exc:  # noqa: BLE001
                print(f"open_about_section click failed: {exc}")
                continue

            return True

    return False


def open_catalog() -> bool:
    """
    Try to find on current page and open "Каталог"/"Продукция" page
    Returns:
        Confirmation bool. Does "Каталог"/"Продукция" page was found and opend successfully.
    """
    contacts_texts = ["Каталог", "продукция", "товары"]

    for link_text in contacts_texts:
        links = find_links(link_text)
        if links:
            try:
                click_link(links[0])
            except:
                continue
            
            return True

    return False


def parse_website(url: str) -> List[str]:
    """
    Open a webpage in browser and find all emails.
    Args:
        url: The page to load.
    Returns:
        List of unique emails.
    """
    visit_website(url)
    emails = get_emails()
    if emails:
        return emails

    contacts_texts = ["контакты", "наши контакты", "связаться с нами", "обратная связь"]

    for link_text in contacts_texts:
        links = find_links(link_text)
        if links:
            try:
                click_link(links[0])
            except:
                continue
            
            emails += get_emails()
            return emails

    return []


def yandex_search_suppliers(query: str) -> List[Dict]:
    """
    Use Yandex Web Search API to get SERP and find websites that are likely
    direct manufacturers or suppliers for the given product query.

    Args:
        query: Text query for search

    Returns:
        The list of search results from page with nested fields - "title", "text", "link".
    """
    # --- Config from env (do not hard-code secrets) ---
    api_key = os.getenv("YANDEX_API_KEY")
    folder_id = os.getenv("YANDEX_FOLDER_ID")

    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",          # full-text web search
            "queryText": query,
            "familyMode": "FAMILY_MODE_NONE",         # adjust as needed: SAFE / MODERATE / NONE
            "page": 0,
            "fixTypoMode": "FIX_TYPO_MODE_OFF",        # typo correction
        },
        "sortSpec": {
            "sortMode": "SORT_MODE_BY_RELEVANCE",      # relevance / date etc., per Yandex docs
            "sortOrder": "SORT_ORDER_DESC",
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": 20,
            "docsInGroup": 1,
        },
        "maxPassages": 3,
        # "region": params.region_id,
        "l10N": "LOCALIZATION_RU",                     # notification language (adapt if needed)
        "folderId": folder_id,
        "responseFormat": "FORMAT_HTML",         # Yandex returns HTML/SEARCH_XML in base64
        "userAgent": "browser-use-supplier-finder/1.0",
    }

    try:
        response = requests.post(
            "https://searchapi.api.cloud.yandex.net/v2/web/search",
            headers={"Authorization": f"Api-Key {api_key}"},
            json=body,
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print('Search API request status not 200')
        return []

    try:
        result_json = response.json()
        raw_data_b64 = result_json.get("rawData")
        if not raw_data_b64:
            print('No data recieved from Search API response')
            return []

        # Decode base64 HTML
        html_bytes = base64.b64decode(raw_data_b64)
        html_text = html_bytes.decode("utf-8", errors="ignore")

        # HTML parsing (TODO: XML)
        soup = BeautifulSoup(html_text, "html.parser")

        parsed_results = []
        serp_items = soup.find_all('li', {'class': "serp-item"})

        for si in serp_items:
            main_link = si.find_all('a', {'class': 'Link'})
            passages = si.find_all('div', {'class': "TextContainer"})
            # Skip "Картинки"
            if main_link[0].text == 'Картинки':
                continue

            parsed_url = urlparse(main_link[0]['href'])
            parsed_results.append({
                "title": main_link[0].text,
                "text": "\n".join(p.text for p in passages),
                "link": f"{parsed_url.scheme}://{parsed_url.netloc}/"
            })

        return parsed_results
    except Exception as exc:  # noqa: BLE001
        print('Seacrh was failed with', exc)
        return []


def doc_validation(technical_spec: str, doc) -> Tuple[bool, str]:
    """
    Use LLM to decide if a single search result is a potentially relevant supplier.

    Args:
        technical_spec: Compact description of the technical task (spec_for_agent)
        text: Search result snippet (title + snippet)

    Returns:
        bool: True if the result looks like a relevant supplier/manufacturer/wholesaler,
              False otherwise (marketplaces, aggregators, irrelevant industries, etc.)
    """
    task = DOC_VAL_INSTRUCTIONS.format(technical_spec=technical_spec, **doc)
    try:
        response = client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=[
                {
                    "role": "system",
                    "content": "Ты эксперт по закупкам и умеешь отбирать релевантных поставщиков по результатам поиска."
                },
                {"role": "user", "content": task},
            ],
            temperature=0.0,
            max_tokens=300,
        )

        raw = response.choices[0].message.content.strip()
        parsed = parse_json_response(raw)

        return bool(parsed.get("is_relevant", False)), parsed.get("reason", "")
    except Exception as e:
        # В случае любой ошибки считаем результат нерелевантным,
        # чтобы не загрязнять выборку случайными сайтами.
        print("doc_validation error:", e)
        return False, "doc_validation error: " + str(e)


def company_validation(
    tz: str,
    website: str,
    main_page_img=None,
    main_page_content: Optional[str] = None,
    about_page_img=None,
    about_page_content: Optional[str] = None,
    catalog_page_img=None,
    catalog_page_content: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Advanced validation of a company website against the technical task.

    Args:
        tz: Compact text version of technical task (build_validation_tz output)
        website: Base URL of the company site
        *_content: Text content (html2text) of main/about/catalog pages
        *_img: screenshots (ignored here, kept for API compatibility)

    Returns:
        dict with keys:
            - is_relevant: bool
            - reason: str
            - name: Optional[str]
            - website: str
    """

    # Combine all available text content
    main_text = (main_page_content or "").strip()
    about_text = (about_page_content or "").strip()
    catalog_text = (catalog_page_content or "").strip()

    site_text_block = "\n\n".join(
        block for block in [
            f"=== ГЛАВНАЯ СТРАНИЦА ===\n{main_text}\n" if main_text else "",
            f"=== РАЗДЕЛ 'О КОМПАНИИ' ===\n{about_text}\n" if about_text else "",
            f"=== РАЗДЕЛ 'КАТАЛОГ / ПРОДУКЦИЯ' ===\n{catalog_text}" if catalog_text else "",
        ] if block
    )

    if not site_text_block:
        site_text_block = "Текстовое содержимое сайта практически отсутствует."

    task = COMPANY_VAL_INSTRUCTIONS.format(tz=tz, site_text_block=site_text_block)
    try:
        response = client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=[
                {
                    "role": "system",
                    "content": "Ты эксперт по закупкам и оцениваешь релевантность поставщиков по содержимому их сайта."
                },
                {"role": "user", "content": task},
            ],
            temperature=0.0,
            max_tokens=400,
        )

        raw = response.choices[0].message.content.strip()
        parsed = parse_json_response(raw)

        # Минимальная защита и заполнение полей по умолчанию
        result = {
            "is_relevant": bool(parsed.get("is_relevant", False)),
            "reason": (parsed.get("reason") or "").strip() or "Нет детального пояснения.",
            "name": (parsed.get("name") or "").strip() or None,
        }
        return result

    except Exception as e:
        print("company_validation error:", e)
        # В случае ошибки считаем сайт нерелевантным, но возвращаем структуру
        return {
            "is_relevant": False,
            "reason": f"Ошибка при анализе сайта: {e}",
            "name": None
        }


def collect_contacts_from_text(
    technical_task_text: str,
    query_docs_limit: Optional[int] = None,
    save_results: bool = False,
) -> Dict[str, Any]:
    """Run the full supplier contact ETL for a given technical task text."""

    tz_summary = summarize_tz_for_single_supplier(technical_task_text)
    search_queries = tz_summary.get("search_queries", [])
    tz_for_validation = build_validation_tz(tz_summary)
    query_docs_limit = query_docs_limit or int(os.environ.get("QUERY_DOCS_LIMIT", "3"))

    search_output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for query in search_queries:
        results = yandex_search_suppliers(query)
        query_docs = 0
        for doc in results:
            website = doc["link"]
            if website in seen:
                continue

            seen.add(website)
            try:
                relevant, reason = doc_validation(tz_for_validation, doc=doc)
            except Exception as exc:  # noqa: BLE001
                continue

            if not relevant:
                continue

            try:
                emails = parse_website(website)
            except Exception:
                emails = []

            search_output.append({"website": website, "emails": emails, "reason": reason})
            query_docs += 1
            if query_docs >= query_docs_limit:
                break

    processed_contacts: List[Dict[str, Any]] = []
    seen = set()
    for contact in tqdm(search_output):
        website = contact["website"]
        if website in seen:
            continue

        main_page_content = ""
        about_page_content = None
        catalog_page_content = None
        about_success = False
        catalog_success = False

        visit_website(website)
        main_page_1 = get_screenshot()
        scroll_page(1000)
        main_page_2 = get_screenshot()
        main_page_content = html2text.html2text(html=get_driver().page_source or "")[:10000]

        try:
            about_success = open_about_section()
        except Exception as exc:  # noqa: BLE001
            print(f"open_about_section failed for {website}: {exc}")
            about_success = False
        if about_success:
            about_page_1 = get_screenshot()
            scroll_page(1000)
            about_page_2 = get_screenshot()
            about_page_content = html2text.html2text(html=get_driver().page_source or "")[:10000]

        catalog_success = open_catalog()
        if catalog_success:
            catalog_page_1 = get_screenshot()
            scroll_page(1000)
            catalog_page_2 = get_screenshot()
            catalog_page_content = html2text.html2text(html=get_driver().page_source or "")[:10000]

        validation_result = company_validation(
            tz_for_validation,
            website=website,
            main_page_img=[main_page_1, main_page_2],
            main_page_content=main_page_content,
            about_page_img=[about_page_1, about_page_1] if about_success else None,
            about_page_content=about_page_content if about_success else None,
            catalog_page_img=[catalog_page_1, catalog_page_2] if catalog_success else None,
            catalog_page_content=catalog_page_content if catalog_success else None,
        )

        processed_contacts.append(
            {
                "is_relevant": bool(validation_result.get("is_relevant", False)),
                "reason": validation_result.get("reason"),
                "name": validation_result.get("name"),
                "website": website,
                "emails": contact.get("emails", []),
            }
        )

        seen.add(website)

    if save_results:
        with open("processed_contacts.json", "w") as f:
            f.write(json.dumps(processed_contacts, ensure_ascii=False))

        with open("search_output.json", "w") as f:
            f.write(json.dumps(search_output, ensure_ascii=False))

    return {
        "queries": search_queries,
        "tech_task_excerpt": technical_task_text[:160],
        "search_output": search_output,
        "processed_contacts": processed_contacts,
    }


if __name__ == "__main__":
    with open("tech_task_example.txt") as f:
        technical_task_text = f.read()

    try:
        result = collect_contacts_from_text(technical_task_text, save_results=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        shutdown_driver()
