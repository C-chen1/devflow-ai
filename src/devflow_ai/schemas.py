import pydantic


class TaskCreateRequest(pydantic.BaseModel):
    title: str
    description: str
    repo:str