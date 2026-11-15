from pydantic import BaseModel


class Health(BaseModel):
    service: str
    status: str
