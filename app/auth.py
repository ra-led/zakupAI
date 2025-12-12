import secrets
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from passlib.context import CryptContext
from sqlmodel import select

from .database import get_session
from .models import SessionToken, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def issue_token(user: User, session) -> SessionToken:
    token = secrets.token_urlsafe(32)
    session_token = SessionToken(token=token, user_id=user.id, created_at=datetime.utcnow())
    session.add(session_token)
    session.commit()
    return session_token


def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    session=Depends(get_session),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token required")

    token_value = authorization.split(" ", 1)[1]
    statement = select(SessionToken, User).where(
        SessionToken.token == token_value,
        SessionToken.user_id == User.id,
    )
    result = session.exec(statement).first()

    if not result:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    _, user = result
    return user
