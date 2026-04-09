from pydantic import BaseModel, EmailStr
from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any, List

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    pharmacy_id: UUID
    user_id: UUID
    username: str
    role: str

class LoginRequest(BaseModel):
    username: str
    password: str

class SyncUploadRequest(BaseModel):
    pharmacy_id: UUID
    changes: List[Dict[str, Any]]  # List of change_log entries
    sync_version: int

class SyncUploadResponse(BaseModel):
    success: bool
    records_processed: int
    conflicts_resolved: int
    new_sync_version: int
    errors: List[str]

class SyncDownloadRequest(BaseModel):
    pharmacy_id: UUID
    since_version: int
    limit: int = 100

class SyncDownloadResponse(BaseModel):
    success: bool
    changes: List[Dict[str, Any]]
    current_version: int
    has_more: bool