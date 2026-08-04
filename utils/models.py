import orientation_core as oc
from lib.model_db import list_db_models


def _resolve_model_paths(settings, document_type):
    """
    Resolve which orientation-model checkpoint(s) to use for a given
    DocumentType. Profiles now live in the SQLite table (managed via
    /api/models), not in config_store settings.
    """
    profiles = list_db_models()
    profile = oc.pick_profile(profiles, document_type)
    if not profile or not profile.get('model_paths'):
        return None, (profile or {}).get('name')
    return profile['model_paths'], profile['name']