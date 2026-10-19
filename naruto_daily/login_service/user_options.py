"""User-selected task options shared by the queue and child-process protocols."""

from typing import Literal, NotRequired, TypedDict, cast


SecretRealmMode = Literal['once', 'all', 'skip']
MainTask = Literal['daily', 'arena_weekly']
ARENA_WEEKLY_SECONDS = 40 * 60


class UserOptions(TypedDict):
    mainTask: NotRequired[MainTask]
    equipmentStamina: bool
    secretRealmMode: SecretRealmMode


_MISSING = object()


def parse_user_options(value: object = _MISSING) -> UserOptions:
    """Normalize legacy omissions, preserving explicit false and rejecting bad data."""
    if value is _MISSING:
        value = {}
    if not isinstance(value, dict) or set(value) - {"mainTask", "equipmentStamina", "secretRealmMode", "secretRealmOnce"}:
        raise ValueError("Invalid user options")
    if 'mainTask' in value and (not isinstance(value['mainTask'], str) or value['mainTask'] not in ('daily', 'arena_weekly')):
        raise ValueError('Invalid main task')
    equipment_stamina = value.get("equipmentStamina", True)
    if type(equipment_stamina) is not bool:
        raise ValueError("Invalid equipment stamina option")
    secret_realm_once = value.get('secretRealmOnce', False)
    if type(secret_realm_once) is not bool:
        raise ValueError('Invalid secret realm once option')
    legacy_mode = 'once' if secret_realm_once else 'all'
    secret_realm_mode = value.get('secretRealmMode', legacy_mode)
    if not isinstance(secret_realm_mode, str) or secret_realm_mode not in ('once', 'all', 'skip'):
        raise ValueError('Invalid secret realm mode')
    if 'secretRealmMode' in value and 'secretRealmOnce' in value and secret_realm_mode != legacy_mode:
        raise ValueError('Conflicting secret realm options')
    result: UserOptions = {"equipmentStamina": equipment_stamina, 'secretRealmMode': cast(SecretRealmMode, secret_realm_mode)}
    if 'mainTask' in value:
        result['mainTask'] = cast(MainTask, value['mainTask'])
    return result
