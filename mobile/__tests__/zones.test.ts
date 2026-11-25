import assert from 'node:assert/strict';
import { test } from 'node:test';

import { zoneFor, zonesOf } from '../src/protocol';

const tgs = [100, 200, 300].map((id) => ({ id, name: `TG ${id}`, priority: 5, emergency: true }));

test('zones from the server keep their order and drop talkgroups the phone may not use', () => {
  const z = zonesOf({ talkgroups: tgs, zones: [{ id: 2, name: 'Ops', talkgroups: [300, 999, 100] }, { id: 3, name: 'Empty', talkgroups: [999] }] });
  assert.deepEqual(z, [{ id: 2, name: 'Ops', talkgroups: [300, 100] }]);
});

test('older servers without zones get one zone with every talkgroup', () => {
  assert.deepEqual(zonesOf({ talkgroups: tgs }), [{ id: 0, name: 'All talkgroups', talkgroups: [100, 200, 300] }]);
});

test('the shown zone follows the selected talkgroup but stays put when it holds it', () => {
  const zones = [{ id: 1, name: 'A', talkgroups: [100, 200] }, { id: 2, name: 'B', talkgroups: [200, 300] }];
  assert.equal(zoneFor(zones, 200, 2), 2);
  assert.equal(zoneFor(zones, 300, 1), 2);
  assert.equal(zoneFor(zones, 100, null), 1);
  assert.equal(zoneFor(zones, null, null), 1);
});
