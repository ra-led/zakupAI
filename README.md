# zakupAI

Tooling for locating supplier contact information (email + phone) for a given
product name and description using the Yandex Search API.

## Setup

We use [uv](https://github.com/astral-sh/uv) for dependency management. Install
and sync the project dependencies (including dev tools):

```bash
uv venv --python 3.11
source .venv/bin/activate
uv sync --all-extras
```

## Required environment

The Yandex Search API needs credentials via environment variables:

- `YANDEX_SEARCH_IAM_TOKEN`: IAM token for the Search API
- `YANDEX_SEARCH_FOLDER_ID`: Cloud folder ID that contains the Search API
  resource

You can set them in your shell session or export them from an `.env` file
before running the CLI.

## Usage

Run the contact finder by providing the product name and description. The agent
will search the web, skip aggregator/marketplace domains, and gather at least
five email+phone pairs.

```bash
uv run zakupai-find-contacts "LED floodlight" "industrial outdoor 150W"
```

You can control the search region (`--region`), requested page (`--page`), and
minimum contacts (`--minimum-contacts`).

## Development

- Code style: run `pre-commit` before pushing changes.

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

- Tests: `uv run pytest`

The codebase uses Pydantic v2 models for I/O structures and avoids marketplace
results to focus on real supplier contact pages.
