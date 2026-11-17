from pydantic import BaseModel, Field


class OrganizationInfo(BaseModel):
    id: str
    name: str
    is_personal: bool = False
    is_visible: bool = True


class OrganizationList(BaseModel):
    items: list[OrganizationInfo] = Field(default_factory=list)
    current_org_id: str | None = None


class OrganizationMembership(BaseModel):
    org_id: str
    user_id: str
    role: str
    permissions: set[str] = Field(default_factory=set)
