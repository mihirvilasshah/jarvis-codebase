const assert = require('node:assert/strict');
const commands = require('./viewer/action-commands.js');
const now = new Date('2026-01-01T00:00:00Z');

assert.deepEqual(commands.parse('Jarvis, add task buy coffee', now),
  {type: 'create', kind: 'task', title: 'buy coffee'});
assert.deepEqual(commands.parse('remind me in 20 minutes to buy coffee', now),
  {type: 'create', kind: 'reminder', title: 'buy coffee', due_at: '2026-01-01T00:20:00.000Z'});
assert.equal(commands.parse('remind me to buy coffee in 2 hours', now).due_at, '2026-01-01T02:00:00.000Z');
assert.equal(commands.parse('postpone task buy coffee by 2 hours', now).type, 'postpone');
assert.equal(commands.parse('complete task buy coffee', now).title, 'buy coffee');
assert.equal(commands.parse('research batteries', now).type, 'research');
for (const command of ['open Firefox browser', 'launch Firefox', 'Hey Jarvis, could you open the Mozilla Firefox browser?']) {
  assert.deepEqual(commands.parse(command, now), {type: 'open_browser', browser: 'firefox', new_tab: false});
}
assert.deepEqual(commands.parse('open a new tab', now), {type: 'open_browser', browser: 'default', new_tab: true});
assert.deepEqual(commands.parse('open another browser', now), {type: 'open_browser', browser: 'default', new_tab: false});
assert.deepEqual(commands.parse('open chrome', now), {type: 'open_browser', browser: 'chrome', new_tab: false});
assert.deepEqual(commands.parse('open new tab in Microsoft Edge', now), {type: 'open_browser', browser: 'edge', new_tab: true});
assert.deepEqual(commands.parse('open Firefox in a new tab', now), {type: 'open_browser', browser: 'firefox', new_tab: true});
for (const command of ['open file explorer', 'Hey Jarvis, could you open the File Explorer?', 'launch windows explorer']) {
  assert.deepEqual(commands.parse(command, now), {type: 'open_file_explorer'});
}
assert.equal(commands.parse('open file explorer to downloads', now).type, 'unknown');
assert.equal(commands.parse('do not open file explorer', now).type, 'unknown');
assert.deepEqual(commands.parse('open apple.com in a new tab', now),
  {type: 'open_url', browser: 'default', new_tab: true, url: 'https://apple.com'});
assert.deepEqual(commands.parse('Jarvis, open apple dot com in a new tab', now),
  {type: 'open_url', browser: 'default', new_tab: true, url: 'https://apple.com'});
assert.deepEqual(commands.parse('visit https://apple.com/mac in Firefox', now),
  {type: 'open_url', browser: 'firefox', new_tab: true, url: 'https://apple.com/mac'});
assert.equal(commands.parse('open', now).type, 'clarify');
assert.equal(commands.parse('open javascript:alert(1)', now).type, 'unknown');
assert.equal(commands.parse('open https://apple.com@evil.com', now).type, 'unknown');
assert.equal(commands.parse('open firefox.com', now).type, 'open_url');
assert.equal(commands.parse('open Firefox with an argument', now).type, 'unknown');
assert.equal(commands.parse('remind me to call tomorrow at 3 pm', now).kind, 'reminder');
assert.equal(commands.parse('remind me to call tomorrow at 25:00', now).due_at, null);
assert.equal(commands.parse('buy coffee', now).type, 'unknown');
assert.equal(commands.localInstant('2026-02-30T12:00'), null);
console.log('Action command tests passed.');
