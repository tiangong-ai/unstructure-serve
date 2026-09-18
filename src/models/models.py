from typing import List, Optional, Tuple

from pydantic import BaseModel


class TextElementWithPageNum(BaseModel):
    text: str
    page_number: int
    type: Optional[str] = None


class ResponseWithPageNum(BaseModel):
    result: List[TextElementWithPageNum]
    txt: Optional[str] = None

    @classmethod
    def from_result(cls, result: List[Tuple[str, int]]):
        items = [TextElementWithPageNum(text=item[0], page_number=item[1]) for item in result]
        return cls(result=items)


class TextElementWithoutPageNum(BaseModel):
    text: str


class ResponseWithoutPageNum(BaseModel):
    result: List[TextElementWithoutPageNum]

    @classmethod
    def from_result(cls, result: List[Tuple[str, int]]):
        items = [TextElementWithoutPageNum(text=item) for item in result]
        return cls(result=items)


class MineruTaskSubmitResponse(BaseModel):
    task_id: str
    state: str


class MineruTaskStatusResponse(BaseModel):
    task_id: str
    state: str
    result: Optional[ResponseWithPageNum] = None
    error: Optional[str] = None
