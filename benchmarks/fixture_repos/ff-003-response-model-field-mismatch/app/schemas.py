from pydantic import BaseModel


class UserResponse(BaseModel):
    id: int
    user_name: str
