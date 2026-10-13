"""User-selected task options shared by the queue and child-process protocols."""

from typing import TypedDict


class UserOptions(TypedDict):
    equipmentStamina: bool
    secretRealmOnce: bool


_MISSING = object()


def parse_user_options(value: object = _MISSING) -> UserOptions:
    """Normalize legacy omissions, preserving explicit false and rejecting bad data."""
    if value is _MISSING:
        value = {}
    if not isinstance(value, dict) or set(value) - {"equipmentStamina", "secretRealmOnce"}:
        raise ValueError("Invalid user options")
    equipment_stamina = value.get("equipmentStamina", True)
    if type(equipment_stamina) is not bool:
        raise ValueError("Invalid equipment stamina option")
    secret_realm_once = value.get('secretRealmOnce', False)
    if type(secret_realm_once) is not bool:
        raise ValueError('Invalid secret realm once option')
    return {"equipmentStamina": equipment_stamina, 'secretRealmOnce': secret_realm_once}
