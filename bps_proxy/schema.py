"""Lightweight validation of common client tool argument constraints."""



def matches(value, schema, root=None, depth=0):
    if schema is True:
        return True
    if schema is False or depth > 32:
        return False
    if not isinstance(schema, dict):
        return True
    root = schema if root is None else root
    ref = schema.get('$ref')
    if isinstance(ref, str) and ref.startswith('#/'):
        target = root
        try:
            for key in ref[2:].split('/'):
                target = target[key.replace('~1', '/').replace('~0', '~')]
        except (KeyError, TypeError):
            return False
        if not matches(value, target, root, depth + 1):
            return False
    for key, check in (('allOf', all), ('anyOf', any)):
        choices = schema.get(key)
        if isinstance(choices, list) and not check(matches(value, item, root, depth + 1) for item in choices):
            return False
    choices = schema.get('oneOf')
    if isinstance(choices, list) and sum(matches(value, item, root, depth + 1) for item in choices) != 1:
        return False
    if 'not' in schema and matches(value, schema['not'], root, depth + 1):
        return False
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        return False
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        return False
    kinds = schema.get('type')
    if isinstance(kinds, str):
        kinds = [kinds]
    checks = {'object': isinstance(value, dict), 'array': isinstance(value, list),
              'string': isinstance(value, str), 'number': type(value) in (int, float),
              'integer': type(value) is int or (type(value) is float and value.is_integer()),
              'boolean': type(value) is bool, 'null': value is None}
    if isinstance(kinds, list) and not any(checks.get(kind, False) for kind in kinds):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])):
            return False
        properties = schema.get('properties', {})
        for key, item in value.items():
            spec = properties.get(key, schema.get('additionalProperties', True))
            if not matches(item, spec, root, depth + 1):
                return False
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', len(value)):
            return False
        if not all(matches(item, schema.get('items', True), root, depth + 1) for item in value):
            return False
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', len(value)):
            return False
        # Complex patterns are left to the client rather than evaluated without a time limit.
    if type(value) in (int, float):
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            return False
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            return False
        if 'exclusiveMaximum' in schema and value >= schema['exclusiveMaximum']:
            return False
    return True
