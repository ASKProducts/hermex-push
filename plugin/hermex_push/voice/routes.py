"""Voice choices mounted behind dashboard authentication at /voice/voices."""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from hermex_push.voice import voices

router = APIRouter(prefix="/voice")


def _profile_home(profile: Optional[str]) -> Path:
    from hermes_constants import get_hermes_home
    if profile in (None, "", "current"):
        return get_hermes_home()
    # Use the same verified host validation/resolution as built-in profile routes.
    from hermes_cli import profiles
    try:
        profiles.validate_profile_name(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not profiles.profile_exists(profile):
        raise HTTPException(status_code=404, detail="Profile does not exist")
    return profiles.get_profile_dir(profile)


class VoiceBody(BaseModel):
    mode: str
    voice: str


@router.get("/voices")
def get_voices(profile: Optional[str] = None) -> dict:
    home = _profile_home(profile)
    return voices.read(home)


@router.put("/voices")
def put_voices(body: VoiceBody, profile: Optional[str] = None) -> dict:
    home = _profile_home(profile)
    if body.mode not in voices.MODES:
        raise HTTPException(status_code=400, detail="Unknown voice mode")
    if body.voice not in voices.choices(body.mode):
        raise HTTPException(status_code=400, detail="Voice not available for this mode")
    return voices.write(home, body.mode, body.voice)
