# Unit Testing Patterns

## Requirements
- **80% minimum coverage** on new/modified code
- One test class per action/trigger/task
- Test happy path + every HTTP error the API can return
- Mock at the appropriate level (see patterns below)
- Use `parameterized` for data-driven tests only when there are multiple test cases — don't use it for a single set of parameters
- Fixtures in `unit_test/responses/` (`.json` or `.json.resp` for legacy)
- For simple single-field inputs, use inline dicts directly (`{"user_id": "user@example.com"}`) rather than `read_file_to_dict` — saves a click when reading tests

## Mocking Strategy

### Client-level mocking (preferred for action tests)
Mock `self.connection.client` helper methods directly. This tests action logic in isolation.
```python
from unittest.mock import MagicMock

class MockGraphApiClient:
    def __init__(self):
        self.get_teams = MagicMock()
        self.get_channels = MagicMock()
        self.get_user_info = MagicMock()
        # ... one MagicMock per client method

class MockBotService:
    def __init__(self):
        self.send_channel_message = MagicMock()
        self.send_chat_message = MagicMock()

class MockConnection:
    def __init__(self):
        self.client = MockGraphApiClient()
        self.bot = MockBotService()
```

Usage in tests:
```python
def test_send_message_to_channel(self):
    self.action.connection.client.get_teams.return_value = [{"id": "123"}]
    self.action.connection.client.get_channels.return_value = [{"id": "456"}]
    self.action.connection.bot.send_channel_message.return_value = {"id": "msg-1"}
    result = self.action.run(test_input)
    self.action.connection.bot.send_channel_message.assert_called_once_with(...)
```

### Request-level mocking (for client/integration tests)
Mock `requests.request` or `requests.post` to test the API client itself.
```python
@patch("requests.request")
def test_auth_failure(self, mock_req):
    mock_req.return_value = MockResponse(401)
    with self.assertRaises(PluginException):
        self.client.get_teams("test")
```

### When to use which
- **Action tests** → mock at client level (fast, isolated, tests action logic)
- **Client tests** → mock at requests level (tests HTTP handling, error mapping, pagination)
- **Never** mock the Connection class itself

## File Structure
- `unit_test/util.py` — `MockResponse` class + `default_connector()` helper
- `unit_test/test_<name>.py` — one per action/trigger/task
- `unit_test/responses/` — mock JSON responses
- `unit_test/payloads/` — test input payloads (optional)

## MockResponse Pattern (current)
```python
class MockResponse:
    def __init__(self, status_code: int, filename: str = None):
        self.status_code = status_code
        self.text = ""
        if filename:
            filepath = os.path.join(os.path.dirname(__file__), "responses", filename)
            with open(filepath) as f:
                self.text = f.read()
    def json(self):
        return json.loads(self.text)
```

## Legacy MockResponse (older plugins using `.json.resp`)
```python
class MockResponse:
    def __init__(self, filename: str, status_code: int, text: str = "Example Text"):
        self.filename = filename
        self.status_code = status_code
        self.text = text
    def json(self):
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), f"responses/{self.filename}.json.resp")) as f:
            return json.load(f)
```

## Test Structure
```python
class TestGetItem(unittest.TestCase):
    def setUp(self):
        self.action = default_connector(GetItem())

    @parameterized.expand([("case_name", {input}, "fixture.json")])
    @patch("requests.request")
    def test_success(self, _name, params, fixture, mock_req):
        mock_req.return_value = MockResponse(200, fixture)
        result = self.action.run(params)
        # assertions...

    @parameterized.expand([("not_found", 404), ("unauthorized", 401)])
    @patch("requests.request")
    def test_errors(self, _name, status, mock_req):
        mock_req.return_value = MockResponse(status)
        with self.assertRaises(PluginException):
            self.action.run(params)
```

## Schema Validation
```python
from jsonschema import validate
validate(self.action.run(params), GetItemOutput.schema)
```

## How the builder runs these tests
```bash
# from the plugin root, which is the working directory
python -m pytest unit_test -q
```

Run them the same way yourself. `python -m` puts the plugin root on `sys.path`, so
`from icon_<name>.actions... import ...` resolves with no path manipulation --
neither a `conftest.py` nor a `sys.path.append` line is needed. Older plugins carry
`sys.path.append(os.path.abspath("../"))` at the top of each test file; it is inert
when invoked this way, so do not add it to new tests.
