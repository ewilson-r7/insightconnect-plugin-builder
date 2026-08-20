# Create Plugin Action

Step-by-step workflow for adding a new action to an existing InsightConnect plugin.

## Prerequisites
- The plugin's directory is your working directory
- Vendor API documentation for the endpoint, supplied under `.builder/reference/`
- The endpoint, HTTP method, required/optional parameters, and response schema

## Steps

### 1. Read Current State
- Read `plugin.spec.yaml` to understand existing actions, types, and connection
- Read `util/` to understand the current API client structure
- Check the current version and determine the correct semver bump (Minor for new action)

### 2. Update plugin.spec.yaml
- Add the action definition with inputs, outputs, types, and examples
- Bump the version (Minor bump for new action)
- Add a `version_history` entry (no quotes around the entry)
- Ensure all outputs have `example` values
- Descriptions must NOT end with a period

### 3. Generate Scaffolding
```bash
insight-plugin refresh
```

### 4. Implement the Action
- Create `action.py` in the generated folder
- Use `Output.FIELD_NAME` constants for all output keys
- Access API via `self.connection.client.<domain_method>()`
- Add guards: `if not results: raise PluginException(...)`
- Wrap return in `clean()` for API response data
- Use `params.get(Input.FIELD, default)` for optional inputs

### 5. Add API Client Method
- Add a domain-specific helper to `util/api.py` or `util/graph_api_client.py`
- Method should encapsulate endpoint construction and response parsing
- Actions call helpers, never `_make_request()` directly

### 6. Write Unit Tests
- Create `unit_test/test_<action_name>.py`
- Mock at client level using `MagicMock()` on `self.connection.client`
- Test happy path + error cases
- Validate output against schema with `jsonschema.validate`
- Target ≥80% coverage

### 7. Validate
```bash
prospector icon_<plugin_name>/ --without-tool pyflakes
python -m pytest unit_test -q
insight-plugin validate
```

### 8. Fix Any Issues
- mccabe > 10: break into helper methods
- bandit: no hardcoded secrets
- pylint: no unused imports, no imports inside functions

## Quality Checks Before Declaring Done
- [ ] All tests pass
- [ ] Prospector clean
- [ ] Plugin validates
- [ ] No `[0]` access without guards
- [ ] No bare string output keys
- [ ] version_history updated
