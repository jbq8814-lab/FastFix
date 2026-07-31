class UserService:
    def list_users(self) -> list[dict[str, int | str]]:
        return [{"id": 1, "name": "Ada"}]

    def get_user(self, user_id: int) -> dict[str, int | str]:
        return {"id": user_id, "name": "Ada"}
