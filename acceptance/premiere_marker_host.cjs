'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const dirname = process.argv[2];
const jsonFile = process.argv[3];
const premiereDir = path.resolve(dirname);

const { parseReviewMarkers } = require(path.join(premiereDir, 'markers.js'));
const { applyReviewMarkers } = require(path.join(premiereDir, 'apply.js'));

const rows = parseReviewMarkers(fs.readFileSync(jsonFile, 'utf8'));
assert.equal(rows.length, 1);
assert.deepEqual(
  { seconds: rows[0].seconds, author: rows[0].author, body: rows[0].body },
  { seconds: 1.25, author: 'Test reviewer', body: '=preserve, this\nnext line' }
);

const id = rows[0].id;
const expectedComments = '=preserve, this\nnext line\n\nCuePorch marker: ' + id;
const markerList = [];
const actions = [];
const createCalls = [];
const getCommentsCalls = [];
let lockCalls = 0;
let transactionCalls = 0;
const transactionLabels = [];
let insideLockedAccess = false;
let insideTransaction = false;

const sequence = { async getEndTime() { return { seconds: 2 }; } };
const markers = {
  getMarkers(filters) {
    assert.deepEqual(filters, ['Comment']);
    assert.equal(insideLockedAccess, true);
    return markerList;
  },
  createAddMarkerAction(name, type, start, duration, comments) {
    assert.equal(insideLockedAccess, true);
    assert.equal(insideTransaction, true);
    createCalls.push([name, type, start, duration, comments]);
    return { name, type, start, duration, comments };
  }
};
const project = {
  async getActiveSequence() { return sequence; },
  lockedAccess(cb) {
    lockCalls += 1;
    const previous = insideLockedAccess;
    insideLockedAccess = true;
    try {
      cb();
    } finally {
      insideLockedAccess = previous;
    }
  },
  executeTransaction(cb, label) {
    transactionCalls += 1;
    transactionLabels.push(label);
    const previous = insideTransaction;
    insideTransaction = true;
    try {
      const compound = {
        addAction(action) {
          actions.push(action);
          markerList.push({
            getComments: () => {
              getCommentsCalls.push(action.comments);
              return action.comments;
            }
          });
        }
      };
      cb(compound);
      return true;
    } finally {
      insideTransaction = previous;
    }
  }
};
const host = {
  Marker: { MARKER_TYPE_COMMENT: 'Comment' },
  Project: { async getActiveProject() { return project; } },
  Markers: { async getMarkers(seq) { assert.equal(seq, sequence); return markers; } },
  TickTime: { createWithSeconds(seconds) { return { seconds }; } }
};

(async () => {
  assert.deepEqual(await applyReviewMarkers(host, rows), { added: 1, skipped: 0 });
  assert.equal(lockCalls, 1);
  assert.deepEqual(transactionLabels, ['Import CuePorch markers']);
  assert.equal(transactionCalls, 1);
  assert.deepEqual(actions, [{
    name: 'Test reviewer',
    type: 'Comment',
    start: { seconds: 1.25 },
    duration: { seconds: 0 },
    comments: expectedComments
  }]);
  assert.deepEqual(createCalls, [['Test reviewer', 'Comment', { seconds: 1.25 }, { seconds: 0 }, expectedComments]]);
  assert.deepEqual(getCommentsCalls, []);

  assert.deepEqual(await applyReviewMarkers(host, rows), { added: 0, skipped: 1 });
  assert.equal(lockCalls, 2);
  assert.equal(transactionCalls, 1);
  assert.equal(actions.length, 1);
  assert.deepEqual(getCommentsCalls, [expectedComments]);

  const snapshot = {
    actions: actions.length,
    lockCalls,
    transactionCalls,
    createCalls: createCalls.length,
    getCommentsCalls: getCommentsCalls.length
  };
  await assert.rejects(
    () => applyReviewMarkers(host, [{ ...rows[0], seconds: 2 }]),
    { message: 'Cannot add marker because the marker time is beyond the end of the sequence.' }
  );
  assert.equal(actions.length, snapshot.actions);
  assert.equal(lockCalls, snapshot.lockCalls);
  assert.equal(transactionCalls, snapshot.transactionCalls);
  assert.equal(createCalls.length, snapshot.createCalls);
  assert.equal(getCommentsCalls.length, snapshot.getCommentsCalls);

  assert.throws(
    () => parseReviewMarkers(JSON.stringify({ schema: 1, version: '1', markers: [rows[0], rows[0]] })),
    { message: `Duplicate marker id: ${id}.` }
  );
})().catch((err) => { console.error(err); process.exit(1); });
