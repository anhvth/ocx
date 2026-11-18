import json
import shutil
import subprocess

import pytest

from ocx.landing import _JS, _token_chart, render_landing


MESSAGES = [
    {"input_tokens": 800, "cached_tokens": 600, "output_tokens": 200},
    {"input_tokens": 200, "cached_tokens": 0, "output_tokens": 200},
    {"input_tokens": 9000, "cached_tokens": None, "output_tokens": 100},
]


def test_token_chart_breakdown():
    chart = _token_chart(MESSAGES)
    assert "Uncached input: <strong>400</strong> (28.6%)" in chart
    assert "Cached input: <strong>600</strong> (42.9%)" in chart
    assert "Output: <strong>400</strong> (28.6%)" in chart
    assert "1,400 tokens · 2 of 3 recent responses" in chart
    assert "conic-gradient(var(--blue) 0.000000% 28.571429%" in chart
    assert 'aria-hidden="true"' in chart


@pytest.mark.parametrize("messages", [
    [],
    [{"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}],
    [{"input_tokens": 10, "cached_tokens": None, "output_tokens": 1}],
    [{"input_tokens": 10, "cached_tokens": 20, "output_tokens": 1}],
])
def test_token_chart_empty_or_incomplete(messages):
    chart = _token_chart(messages)
    assert "No complete token usage reported yet." in chart
    assert "conic-gradient" not in chart


def test_token_chart_all_cached():
    chart = _token_chart([{"input_tokens": 100, "cached_tokens": 100, "output_tokens": 0}])
    assert "Cached input: <strong>100</strong> (100.0%)" in chart
    assert "100 tokens · 1 of 1 recent responses" in chart


def test_landing_includes_chart_empty_state():
    page = render_landing("1", "http://localhost/v1", [])
    assert 'id="token-chart"' in page
    assert "Token breakdown · latest 100 responses" in page
    assert "No complete token usage reported yet." in page


@pytest.mark.skipif(not shutil.which("node"), reason="Node required to check dashboard JavaScript")
def test_token_chart_live_refresh():
    # Run the shipped script with a minimal DOM, then trigger the refresh button.
    harness = """
const assert = require('node:assert/strict');
const elements = {
  'token-chart': {innerHTML: ''},
  'refresh': {classList: {add(){}, remove(){}}, addEventListener(_, cb){this.click=cb;}}
};
global.window = {matchMedia(){return {matches: true};}};
global.document = {querySelectorAll(){return [];}, getElementById(id){return elements[id];}};
global.setInterval = () => {};
global.setTimeout = (cb) => cb();
let messages = MESSAGES;
global.fetch = async () => ({ok: true, json: async () => ({messages})});
SCRIPT
(async () => {
  elements.refresh.click();
  await new Promise(setImmediate);
  const chart = elements['token-chart'].innerHTML;
  assert.ok(chart.includes('Cached input: <strong>600</strong> (42.9%)'));
  assert.ok(chart.includes('1,400 tokens · 2 of 3 recent responses'));
  assert.ok(chart.includes('conic-gradient(var(--blue) 0.000000% 28.571429%'));
  messages = [];
  elements.refresh.click();
  await new Promise(setImmediate);
  assert.ok(elements['token-chart'].innerHTML.includes('No complete token usage reported yet.'));
})().catch(e => {console.error(e); process.exitCode=1;});
""".replace("MESSAGES", json.dumps(MESSAGES)).replace("SCRIPT", _JS)
    subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
