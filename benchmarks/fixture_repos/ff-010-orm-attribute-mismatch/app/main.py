from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import User
from app.schemas import UserCreate, UserResponse
from app.service import create_user, get_user, list_users

Base.metadata.create_all(bind=engine)

app = FastAPI()
DatabaseSession = Annotated[Session, Depends(get_db)]


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users", response_model=list[UserResponse])
def list_users_endpoint(db: DatabaseSession) -> list[User]:
    return list_users(db)


@app.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user_endpoint(payload: UserCreate, db: DatabaseSession) -> User:
    return create_user(db, payload.name)


@app.get("/users/{user_id}", response_model=UserResponse)
def get_user_endpoint(user_id: int, db: DatabaseSession) -> User:
    user = get_user(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user
