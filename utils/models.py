def _resolve_model_paths(settings, document_type):
    profiles = settings.get('orientation_models') or []
    profile = oc.pick_profile(profiles, document_type)
    if not profile or not profile.get('model_paths'):
        return None, (profile or {}).get('name')
    return profile['model_paths'], profile['name']
