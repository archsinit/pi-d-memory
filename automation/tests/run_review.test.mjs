import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, readFile, stat, writeFile, rm, realpath, symlink, link } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { Readable } from 'node:stream';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  sha256, parseJson, verifyRuntime, validateRuntime, validateJob, readOnlyCredentials, subscriptionModels,
  fetchAllSources, buildSnapshots, createNoteReader, createReadExtension, validateProposal,
  runReview, main, PAGE_BYTES, PAGE_ENTRIES, NOTE_PAGE_CHARS,
} from '../run_review.mjs';

// This checkout is used READ-ONLY for pinned stock dependencies and auth SOURCE only.
// Never instantiate the default live credential store or read an actual auth file.
// Refresh tests instantiate the pinned adapter ONLY with an explicit temporary file.
const PI_D_ROOT = path.resolve(process.env.TEST_PI_D_ROOT || fileURLToPath(new URL('../../../pi-d', import.meta.url)));
const authHash = sha256(await readFile(path.join(PI_D_ROOT, 'src/auth.ts')));
const runtime = { version: 1, pi_d_root: PI_D_ROOT, auth_module_sha256: authHash,
  model: { provider: 'faux', modelId: 'faux-1' }, thinking_level: 'off', allow_auth_refresh: false };
const loaded = await verifyRuntime(runtime);
const { createModels, fauxProvider, fauxAssistantMessage, fauxToolCall } = await import(
  pathToFileURL(path.join(PI_D_ROOT, 'node_modules/@earendil-works/pi-ai/dist/index.js')).href);
const NOTE = '---\ntype: preference\ntopics: [test, durable]\nsummary: Offline fixture.\n---\n\n# Example\nOriginal durable fact.\n';
const noChange = () => ({ status: 'complete', edits: [], cross_links: [], summary: 'No durable changes needed.' });
const answer = (value) => fauxAssistantMessage(typeof value === 'string' ? value : JSON.stringify(value));
const clone = (value) => structuredClone(value);

async function fixture(t, { after = 0, through = 7, messages, sourceCount = 1 } = {}) {
  const root = await mkdtemp(path.join(await realpath(tmpdir()), 'restricted-review-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const owner = { id: 'planning', workspace: path.join(root, 'planning'), owned_paths: ['memory'],
    indexes: ['memory/index.md'], state_files: [], require_metadata: true, enabled: true };
  const peer = { id: 'hyatt', workspace: path.join(root, 'hyatt'), owned_paths: ['Projects'],
    indexes: ['Projects/README.md'], state_files: [], require_metadata: false, enabled: true };
  await mkdir(path.join(owner.workspace, 'memory'), { recursive: true });
  await mkdir(path.join(owner.workspace, 'state'));
  await mkdir(path.join(peer.workspace, 'Projects'), { recursive: true });
  await writeFile(path.join(owner.workspace, 'AGENTS.md'), 'Owner instructions: preserve provenance.\n');
  await writeFile(path.join(owner.workspace, 'memory', 'AGENTS.md'), 'Scoped memory instructions: retain uncertainty.\n');
  await writeFile(path.join(owner.workspace, 'memory/index.md'), NOTE);
  await writeFile(path.join(owner.workspace, 'memory/topic.md'), NOTE);
  await writeFile(path.join(peer.workspace, 'Projects/README.md'), '# Peer index\n[Hotel](hotel.md)\n');
  await writeFile(path.join(peer.workspace, 'Projects/hotel.md'), '# Hotel\nPeer-owned facts, never rewrite.\n');
  await writeFile(path.join(peer.workspace, 'private.md'), 'OUTSIDE_PEER_ALLOWLIST');
  await writeFile(path.join(owner.workspace, 'private.md'), 'OUTSIDE_OWNER_ALLOWLIST');
  const history_helper = path.join(root, 'history.py');
  await writeFile(history_helper, '# test fixture, never a live history source\n');
  const policy_file = path.join(root, 'policy.md');
  await writeFile(policy_file, '# Shared test policy\nReturn complete JSON proposals only; never write files.\n');
  const sources = [];
  for (let index = 0; index < sourceCount; index++) {
    const source = { state_file: path.join(owner.workspace, `state/review-${index}.json`), database: path.join(root, `fake-${index}.sqlite`),
      conversation_id: 11 + index, after_entry: after, through_entry: through };
    await writeFile(source.database, 'OFFLINE_FAKE_DATABASE_NOT_SQLITE');
    await writeFile(source.state_file, JSON.stringify({ version: 1, mode: 'history-reference',
      source: { database: source.database, conversation_id: source.conversation_id }, last_saved_entry_id: after,
      last_saved_review: null, active_review: null }));
    owner.state_files.push(source.state_file); sources.push(source);
  }
  const job = { version: 1, owner, peers: [peer], history_helper, policy_file, sources, mode: 'capture' };
  const helperCalls = [];
  const helperCall = async (request) => {
    helperCalls.push(request);
    return page(request, { messages: messages ?? (after < through ? [{ entry_id: through, role: 'user', text_blocks: ['Remember a durable preference.'] }] : []) });
  };
  return { root, owner, peer, job, helperCall, helperCalls };
}
function page({ source, after, through }, { end = through, start = after < through ? through : null,
  count = after < through ? 1 : 0, messages = [], commit = 10 } = {}) {
  return { format: 'pi-d-history-v1', database: source.database, conversation_id: source.conversation_id,
    schema_version: 1, observed_commit_seq: commit, through_entry: through, after_entry: after,
    processed_from_entry: start, processed_through_entry: end, processed_entries: count, next_after_entry: end,
    has_more: end < through, last_user_entry: messages.filter((message) => message.role === 'user').at(-1)?.entry_id ?? null, messages };
}
function faux(responses) {
  const provider = fauxProvider({ tokensPerSecond: 0, tokenSize: { min: 4096, max: 4096 } });
  provider.setResponses(responses);
  const models = createModels(); models.setProvider(provider.provider);
  return { provider, models };
}
async function run(f, responses = [answer(noChange())], options = {}) {
  const { provider, models } = faux(responses);
  let closed = false; const openings = [];
  const openHarness = async (storage, settings, context) => {
    openings.push({ storage, settings });
    const harness = await loaded.sdk.Harness.open(storage, settings, context);
    harness.closed.then(() => { closed = true; });
    return harness;
  };
  const result = await runReview(f.job, runtime, { loadRuntime: async () => loaded, helperCall: f.helperCall,
    makeModels: () => models, openHarness, ...options });
  return { result, provider, openings, closed };
}
const editFor = (relative = 'memory/new.md', content = NOTE, expected_sha256 = null) => ({ path: relative, expected_sha256, content });
function toolResult(context, name) { return context.messages.filter((message) => message.role === 'toolResult' && message.toolName === name).at(-1); }
function toolJson(message) { return JSON.parse(message.content.filter((block) => block.type === 'text').map((block) => block.text).join('')); }
async function subprocess(args, input = '', executable = process.execPath) {
  const child = spawn(executable, args, { stdio: ['pipe', 'pipe', 'pipe'] });
  const stdout = []; const stderr = [];
  child.stdout.on('data', (chunk) => stdout.push(chunk)); child.stderr.on('data', (chunk) => stderr.push(chunk));
  child.stdin.end(input);
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  return { code, stdout: Buffer.concat(stdout).toString(), stderr: Buffer.concat(stderr).toString() };
}

test('actual pinned SDK and --check import safely without constructing credentials/model work', async (t) => {
  assert.equal(typeof loaded.sdk.Harness.open, 'function');
  const f = await fixture(t);
  const config = path.join(f.root, 'runtime.json'); await writeFile(config, JSON.stringify(runtime));
  const cli = await subprocess([fileURLToPath(new URL('../run_review.mjs', import.meta.url)), '--runtime', config, '--check']);
  assert.equal(cli.code, 0); assert.equal(cli.stderr, '');
  const result = parseJson(cli.stdout); assert.equal(result.status, 'complete'); assert.deepEqual(result.coverage, []);
  assert.match(result.summary, /no credentials or model calls/);
  let models = 0; let credentials = 0; let output = '';
  await main(['--runtime', config, '--check'], { stdout: { write: (text) => { output += text; } },
    loadRuntime: async (input) => {
      assert.deepEqual(input, runtime);
      return { ...loaded, auth: { SubscriptionCredentials: class { constructor() { credentials++; } }, chatModels() { models++; } } };
    }, makeModels: () => { models++; } });
  assert.equal(credentials, 0); assert.equal(models, 0); assert.equal(JSON.parse(output).status, 'complete');
});

test('auth source pin and SDK version mismatches fail before ANY import', async (t) => {
  let imports = 0;
  await assert.rejects(verifyRuntime({ ...runtime, auth_module_sha256: '0'.repeat(64) }, { importModule: async () => { imports++; } }), /auth_hash_mismatch/);
  assert.equal(imports, 0);
  const f = await fixture(t);
  const root = path.join(f.root, 'fake-host'); const dependency = path.join(root, 'node_modules/@earendil-works/pi-durable');
  await mkdir(dependency, { recursive: true }); await writeFile(path.join(root, 'package.json'), '{}');
  await writeFile(path.join(dependency, 'package.json'), JSON.stringify({ name: '@earendil-works/pi-durable', version: '1.2.1' }));
  await assert.rejects(verifyRuntime({ ...runtime, pi_d_root: root }, { importModule: async () => { imports++; } }), /dependency_version_mismatch/);
  assert.equal(imports, 0);
  for (const bad of [{ ...runtime, allow_auth_refresh: 'true' }, { ...runtime, allow_auth_refresh: null },
    { ...runtime, allow_auth_refresh: 1 }, { ...runtime, tool_budget: 10 }, { ...runtime, pi_d_root: './host' }]) {
    assert.throws(() => validateRuntime(bad));
  }
});

test('case and Unicode-normalization aliases cannot create duplicate proposed notes', async (t) => {
  const f = await fixture(t); const snapshots = await buildSnapshots(f.job); const reader = createNoteReader(snapshots);
  for (const paths of [['memory/New.md', 'memory/new.md'], ['memory/caf\u00e9.md', 'memory/cafe\u0301.md']]) {
    assert.throws(() => validateProposal(JSON.stringify({ ...noChange(), edits: paths.map((name) => editFor(name)) }),
      f.job, snapshots, reader), /duplicate_edit/);
  }
});

test('strict JSON rejects duplicates, trailing/truncated text and non-finite numbers', () => {
  for (const text of ['{"x":1,"x":2}', '{} trailing', '{"x":', 'NaN', '1e999', '[1,]', '{"x":1,}', '```json\n{}\n```']) assert.throws(() => parseJson(text));
  assert.equal(parseJson('{"__proto__":{"ok":true}}').__proto__.ok, true);
  assert.equal({}.ok, undefined);
});

test('capture with no eligible text never constructs models or credentials, even with zero-range source', async (t) => {
  for (const config of [{ after: 7, through: 7 }, { after: 0, through: 7, messages: [] },
    { after: 0, through: 7, messages: [{ entry_id: 7, role: 'user', text_blocks: [' \n\t'] }] }, { after: 0, through: 0 }]) {
    const f = await fixture(t, config); let models = 0;
    const { result, provider, openings } = await run(f, [], { makeModels: () => { models++; throw new Error('must not happen'); } });
    assert.equal(result.status, 'complete'); assert.equal(models, 0); assert.equal(provider.state.callCount, 0); assert.equal(openings.length, 0);
    assert.equal(f.helperCalls.length, 1); assert.equal(result.coverage[0].processed_through_entry, config.through);
    assert.deepEqual(result.edits, []);
  }
});

test('zero-source capture is also no-model; explicit audit may call native reviewer', async (t) => {
  const f = await fixture(t, { sourceCount: 0 });
  const ordinary = await run(f); assert.equal(ordinary.result.status, 'complete'); assert.equal(ordinary.provider.state.callCount, 0);
  f.job.mode = 'audit';
  const audit = await run(f); assert.equal(audit.result.status, 'complete'); assert.equal(audit.provider.state.callCount, 1); assert.equal(audit.closed, true);
});

test('native no-change settles/closes stock MemoryStorage and exposes exactly two read tools, no env or budgets', async (t) => {
  const f = await fixture(t); let seen = false;
  const done = await run(f, [(context, options) => {
    seen = true; assert.equal(options.maxRetries, 0);
    const serialized = JSON.stringify(context.messages);
    assert.match(serialized, /Owner instructions: preserve provenance/);
    assert.match(serialized, /Scoped memory instructions: retain uncertainty/);
    assert.match(serialized, /First owner index page/);
    assert.doesNotMatch(serialized, /Peer-owned facts|OUTSIDE_PEER_ALLOWLIST|OUTSIDE_OWNER_ALLOWLIST/);
    const tools = context.messages.flatMap((message) => message.role === 'system' ? message.toolsAdded ?? [] : []);
    assert.deepEqual(tools.map((tool) => tool.name).sort(), ['list_notes', 'read_note']);
    return answer(noChange());
  }]);
  assert.equal(seen, true); assert.equal(done.result.status, 'complete'); assert.equal(done.closed, true);
  assert.deepEqual(done.result.coverage, [{ ...f.job.sources[0], processed_through_entry: 7 }]);
  const [{ storage, settings }] = done.openings;
  assert.ok(storage instanceof loaded.sdk.MemoryStorage); assert.equal(Object.hasOwn(settings, 'env'), false);
  assert.equal(settings.settings.compaction.enabled, false); assert.equal(settings.settings.compaction.backgroundTokens, 0);
  assert.equal(settings.settings.retry.enabled, false); assert.equal(settings.settings.retry.maxRetries, 0);
  assert.deepEqual(settings.registry.snapshot().installed().map((extension) => extension.name), ['restricted-memory-review']);
  assert.deepEqual(settings.registry.snapshot().tools().map(({ tool }) => tool.name), ['list_notes', 'read_note']);
  assert.deepEqual(Object.keys(settings.settings).sort(), ['compaction', 'extensions', 'retry', 'stream']);
});

test('model attempts to invoke bash/edit/write/delegation have no executable capability', async (t) => {
  const f = await fixture(t); const before = await readFile(f.owner.workspace + '/memory/topic.md');
  const done = await run(f, [fauxAssistantMessage([
    fauxToolCall('bash', { command: 'NOT_A_REAL_COMMAND' }),
    fauxToolCall('edit', { path: 'memory/topic.md', content: 'UNAUTHORIZED' }),
    fauxToolCall('write', { path: 'memory/new.md', content: 'UNAUTHORIZED' }),
    fauxToolCall('subagent', { task: 'UNAUTHORIZED' }),
  ], { stopReason: 'toolUse' }), (context) => {
    const results = context.messages.filter((message) => message.role === 'toolResult');
    assert.equal(results.length, 4); assert.ok(results.every((message) => message.isError));
    return answer(noChange());
  }]);
  assert.equal(done.result.status, 'complete'); assert.equal(done.closed, true);
  assert.deepEqual(await readFile(f.owner.workspace + '/memory/topic.md'), before);
  await assert.rejects(readFile(f.owner.workspace + '/memory/new.md'), { code: 'ENOENT' });
});

test('native read/search, full owner replacement, new note and structured foreign cross-link are proposal-only', async (t) => {
  const f = await fixture(t);
  const beforeState = await readFile(f.job.sources[0].state_file);
  const beforePeer = await readFile(path.join(f.peer.workspace, 'Projects/hotel.md'));
  const existingHash = sha256(Buffer.from(NOTE));
  const expected = { status: 'complete', edits: [editFor('memory/topic.md', NOTE.replace('Original durable fact.', 'Corrected durable fact.'), existingHash), editFor()],
    cross_links: [{ destination_agent: 'hyatt', destination_path: 'Projects/hotel.md', target_agent: 'planning', target_path: 'memory/new.md' }], summary: 'Proposed one correction, one note and an additive link.' };
  const done = await run(f, [
    fauxAssistantMessage(fauxToolCall('list_notes', { agent: 'planning', query: 'durable', limit: 1 }), { stopReason: 'toolUse' }),
    (context) => {
      const listing = toolJson(toolResult(context, 'list_notes')); assert.equal(listing.notes.length, 1); assert.equal(listing.next_cursor, 1);
      assert.equal(Object.hasOwn(listing.notes[0], 'content'), false); assert.equal(listing.notes[0].sha256, existingHash);
      return fauxAssistantMessage([fauxToolCall('read_note', { agent: 'planning', path: 'memory/topic.md' }),
        fauxToolCall('read_note', { agent: 'hyatt', path: 'Projects/hotel.md' })], { stopReason: 'toolUse' });
    },
    (context) => { assert.match(toolJson(toolResult(context, 'read_note')).content, /Peer-owned/); return answer(expected); },
  ]);
  assert.equal(done.result.status, 'complete'); assert.equal(done.closed, true); assert.equal(done.provider.state.callCount, 3);
  assert.deepEqual(done.result.edits, expected.edits); assert.deepEqual(done.result.cross_links, expected.cross_links);
  assert.equal(await readFile(path.join(f.owner.workspace, 'memory/topic.md'), 'utf8'), NOTE);
  assert.deepEqual(await readFile(f.peer.workspace + '/Projects/hotel.md'), beforePeer);
  assert.deepEqual(await readFile(f.job.sources[0].state_file), beforeState);
  await assert.rejects(readFile(path.join(f.owner.workspace, 'memory/new.md')), { code: 'ENOENT' });
});

test('frozen multi-page multi-source reads ALL pages before the model; coverage is wrapper-generated', async (t) => {
  const f = await fixture(t, { sourceCount: 2, through: 20 }); const reads = [];
  f.helperCall = async (request) => {
    reads.push([request.source.conversation_id, request.after, request.through]);
    return page(request, { start: request.after === 0 ? 2 : 20, end: request.after === 0 ? 7 : 20,
      count: request.after === 0 ? 2 : 1, commit: request.after === 0 ? 10 : 12,
      messages: [{ entry_id: request.after === 0 ? 2 : 20, role: 'user', text_blocks: [`frozen-${request.source.conversation_id}-${request.after}`] }] });
  };
  const done = await run(f, [(context) => {
    assert.deepEqual(reads, [[11, 0, 20], [11, 7, 20], [12, 0, 20], [12, 7, 20]]);
    const text = JSON.stringify(context.messages); assert.match(text, /frozen-11-0/); assert.match(text, /frozen-12-7/);
    return answer(noChange());
  }]);
  assert.equal(done.result.status, 'complete');
  assert.deepEqual(done.result.coverage, f.job.sources.map((source) => ({ ...source, processed_through_entry: 20 })));
});

test('source identity/bounds/truncated/nonprogress pages and failed reads block BEFORE model admission', async (t) => {
  const mutations = [
    (p) => { p.database = '/foreign/database'; }, (p) => { p.conversation_id++; }, (p) => { p.schema_version = 2; },
    (p) => { p.after_entry++; }, (p) => { p.through_entry++; }, (p) => { p.has_more = true; },
    (p) => { p.processed_through_entry = p.after_entry; p.next_after_entry = p.after_entry; },
    (p) => { delete p.messages; }, (p) => { p.messages[0].role = 'toolResult'; }, (p) => { p.messages[0].entry_id = 99; },
    (p) => { p.truncated = true; }, (p) => { p.observed_commit_seq = -1; },
  ];
  for (const mutation of mutations) {
    const f = await fixture(t); const normal = f.helperCall;
    f.helperCall = async (request) => { const p = await normal(request); mutation(p); return p; };
    const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(done.provider.state.callCount, 0); assert.equal(done.openings.length, 0);
    assert.equal(done.result.coverage[0].processed_through_entry, 0); assert.deepEqual(done.result.edits, []);
  }
  const f = await fixture(t); f.helperCall = async () => { throw new Error('SECRET_RAW_HISTORY_ERROR'); };
  const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(done.provider.state.callCount, 0);
  assert.doesNotMatch(JSON.stringify(done.result), /SECRET/);
});

test('regressing observed commit sequence and partial second source block without calling models', async (t) => {
  const f = await fixture(t, { through: 20, sourceCount: 2 });
  f.helperCall = async (request) => page(request, { start: request.after === 0 ? 7 : 20, end: request.after === 0 ? 7 : 20,
    commit: request.after === 0 ? 10 : 9, messages: [] });
  const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(done.provider.state.callCount, 0);
  assert.equal(done.result.coverage[0].processed_through_entry, 7); assert.equal(done.result.coverage[1].processed_through_entry, 0);
});

test('state/source mismatch, active legacy writer and cross-peer history are rejected before helper/model', async (t) => {
  for (const mutation of [
    (s) => { s.source.database += '.other'; }, (s) => { s.source.conversation_id++; },
    (s) => { s.last_saved_entry_id++; }, (s) => { s.active_review = { id: 'old' }; },
  ]) {
    const f = await fixture(t); const source = f.job.sources[0]; const state = JSON.parse(await readFile(source.state_file, 'utf8'));
    mutation(state); await writeFile(source.state_file, JSON.stringify(state));
    const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(f.helperCalls.length, 0); assert.equal(done.provider.state.callCount, 0);
  }
  const f = await fixture(t); f.peer.state_files = [path.join(f.peer.workspace, 'foreign-state.json')]; f.job.sources[0].state_file = f.peer.state_files[0];
  const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(f.helperCalls.length, 0); assert.equal(done.provider.state.callCount, 0);
});

test('state or database identity changing during fetch blocks; no externally applied checkpoint', async (t) => {
  const f = await fixture(t); const normal = f.helperCall;
  f.helperCall = async (request) => {
    const result = await normal(request); await writeFile(request.source.state_file, '{}'); return result;
  };
  const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.match(done.result.summary, /source_state_changed/); assert.equal(done.provider.state.callCount, 0);
});

test('configured history Python is used for every page with no fallback or model work', async (t) => {
  const f = await fixture(t); const source = f.job.sources[0]; const calls = path.join(f.root, 'interpreter-calls.jsonl');
  const python = path.join(f.root, 'approved python3.14.mjs');
  const pages = [page({ source, after: 0, through: 7 }, { start: 3, end: 3, messages: [] }),
    page({ source, after: 3, through: 7 }, { start: 7, end: 7, messages: [] })];
  await writeFile(python, `#!${process.execPath}\nimport { appendFile } from 'node:fs/promises';\nconst args=process.argv.slice(2);\nawait appendFile(${JSON.stringify(calls)},JSON.stringify(args)+'\\n');\nconst after=Number(args[args.indexOf('--after-entry')+1]);\nconsole.log(JSON.stringify(${JSON.stringify(pages)}.find((p)=>p.after_entry===after)));\n`, { mode: 0o700 });
  let models = 0;
  const options = { loadRuntime: async (input) => ({ ...loaded, runtime: input }),
    makeModels: () => { models++; throw new Error('No eligible text must not construct models'); } };
  const selected = { ...runtime, history_python: python };
  const result = await runReview(f.job, selected, options);
  assert.equal(result.status, 'complete', result.summary); assert.equal(models, 0);
  assert.equal(result.coverage[0].processed_through_entry, 7);
  const actual = (await readFile(calls, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(actual.length, 2);
  for (const [index, args] of actual.entries()) {
    assert.deepEqual(args.slice(0, 3), ['-B', f.job.history_helper, 'read']);
    assert.equal(args[args.indexOf('--db') + 1], source.database);
    assert.equal(args[args.indexOf('--after-entry') + 1], index === 0 ? '0' : '3');
    assert.equal(args[args.indexOf('--through-entry') + 1], '7');
  }
  const missing = await runReview(f.job, { ...selected, history_python: path.join(f.root, 'missing-python') }, options);
  assert.equal(missing.status, 'blocked'); assert.equal(models, 0);
  assert.equal((await readFile(calls, 'utf8')).trim().split('\n').length, 2);
  assert.throws(() => validateRuntime({ ...runtime, history_python: 'python3.14' }));
  assert.throws(() => validateRuntime({ ...runtime, history_python: 314 }));
  let imports = 0;
  await assert.rejects(verifyRuntime({ ...runtime, history_python: path.join(f.root, 'missing-python') },
    { importModule: async () => { imports++; } }), /history_python_unavailable/);
  assert.equal(imports, 0);
});

test('actual helper subprocess receives fixed bounds, follows paging, rejects truncated stdout and sanitizes stderr', async (t) => {
  const f = await fixture(t, { through: 20 });
  await writeFile(f.job.history_helper, `import argparse,json\np=argparse.ArgumentParser()\np.add_argument('command')\nfor name in ['db','conversation','after-entry','through-entry','limit','max-bytes']: p.add_argument('--'+name,required=True)\na=p.parse_args()\nassert a.command=='read' and int(a.limit)==${PAGE_ENTRIES} and int(a.max_bytes)==${PAGE_BYTES}\nafter=int(a.after_entry); through=int(a.through_entry); end=7 if after==0 else through\nprint(json.dumps({'format':'pi-d-history-v1','database':a.db,'conversation_id':int(a.conversation),'schema_version':1,'observed_commit_seq':10,'through_entry':through,'after_entry':after,'processed_from_entry':end,'processed_through_entry':end,'processed_entries':1,'next_after_entry':end,'has_more':end<through,'last_user_entry':end,'messages':[{'entry_id':end,'role':'user','text_blocks':['OFFLINE_PYTHON_HELPER']}]}))\n`);
  const fetched = await fetchAllSources(validateJob(f.job));
  assert.equal(fetched.coverage[0].processed_through_entry, 20); assert.equal(fetched.evidence[0].messages.length, 2);
  await writeFile(f.job.history_helper, 'import sys\nprint(\'{"format":\')\nprint("SECRET_STDERR",file=sys.stderr)\n');
  const broken = await run(f, [], { helperCall: undefined }); assert.equal(broken.result.status, 'blocked'); assert.equal(broken.provider.state.callCount, 0);
  assert.doesNotMatch(JSON.stringify(broken.result), /SECRET|format/);
  await writeFile(f.job.history_helper, 'import sys\nprint("SECRET_PROVIDER_ERROR",file=sys.stderr)\nsys.exit(1)\n');
  const failed = await run(f, [], { helperCall: undefined }); assert.equal(failed.result.status, 'blocked'); assert.doesNotMatch(JSON.stringify(failed.result), /SECRET/);
});

test('existing read_conversation.py consumes a synthetic native SQLite multi-page range, excludes newer/foreign entries, never writes it', async (t) => {
  const f = await fixture(t, { through: 80 }); const source = f.job.sources[0];
  const schema = fileURLToPath(new URL('../../helpers/tests/native_schema_v1.sql', import.meta.url));
  await rm(source.database);
  const setup = await subprocess(['-B', '-c', `import json,sqlite3,sys\nfrom pathlib import Path\nc=sqlite3.connect(sys.argv[1]); c.executescript(Path(sys.argv[2]).read_text())\nfor cid in [11,12]:\n c.execute("INSERT INTO record_ids VALUES (?, 'conversation')",(cid,)); c.execute("INSERT INTO conversations VALUES (?,NULL,NULL,?)",(cid,json.dumps({'id':cid})))\nseq=0\nfor eid,cid,text in [(eid,11,'FROZEN_ENTRY_'+str(eid)) for eid in range(20,81)]+[(81,11,'NEWER_ENTRY_MUST_NOT_APPEAR'),(5000,12,'FOREIGN_HISTORY_MUST_NOT_APPEAR')]:\n seq+=1; record={'id':eid,'conversationId':cid,'kind':'pi.user','model':[{'role':'user','content':text,'timestamp':1}]}\n c.execute("INSERT INTO record_ids VALUES (?, 'entry')",(eid,)); c.execute("INSERT INTO entries VALUES (?,?,NULL,?,?)",(eid,cid,seq,json.dumps(record)))\nc.execute('UPDATE durable_metadata SET next_id=?,next_seq=?',('5001',seq+1)); c.commit(); c.close()\n`, source.database, schema], '', '/usr/bin/python3');
  assert.equal(setup.code, 0, setup.stderr);
  f.job.history_helper = fileURLToPath(new URL('../../helpers/read_conversation.py', import.meta.url));
  const before = await readFile(source.database); let promptSeen = false;
  const done = await run(f, [(context) => {
    const prompt = JSON.stringify(context.messages); assert.match(prompt, /FROZEN_ENTRY_20/); assert.match(prompt, /FROZEN_ENTRY_80/);
    assert.doesNotMatch(prompt, /NEWER_ENTRY_MUST_NOT_APPEAR|FOREIGN_HISTORY_MUST_NOT_APPEAR/);
    promptSeen = true; return answer(noChange());
  }], { helperCall: undefined });
  assert.equal(done.result.status, 'complete'); assert.equal(promptSeen, true); assert.equal(done.result.coverage[0].processed_through_entry, 80);
  assert.deepEqual(await readFile(source.database), before);
});

test('CLI capture reads exactly one JSON job, no model work for empty eligible history, stdout JSON only', async (t) => {
  const f = await fixture(t, { after: 0, through: 0 });
  await writeFile(f.job.history_helper, `import argparse,json\np=argparse.ArgumentParser()\np.add_argument('command')\nfor name in ['db','conversation','after-entry','through-entry','limit','max-bytes']: p.add_argument('--'+name,required=True)\na=p.parse_args()\nprint(json.dumps({'format':'pi-d-history-v1','database':a.db,'conversation_id':int(a.conversation),'schema_version':1,'observed_commit_seq':0,'through_entry':0,'after_entry':0,'processed_from_entry':None,'processed_through_entry':0,'processed_entries':0,'next_after_entry':0,'has_more':False,'last_user_entry':None,'messages':[]}))\n`);
  const config = path.join(f.root, 'runtime.json'); await writeFile(config, JSON.stringify(runtime));
  const executable = fileURLToPath(new URL('../run_review.mjs', import.meta.url));
  const cli = await subprocess([executable, '--runtime', config], JSON.stringify(f.job));
  assert.equal(cli.code, 0); assert.equal(cli.stderr, ''); assert.equal(JSON.parse(cli.stdout).status, 'complete');
  const twice = await subprocess([executable, '--runtime', config], '{} {}');
  assert.equal(JSON.parse(twice.stdout).status, 'blocked'); assert.equal(twice.stderr, '');
});

test('owner/peer snapshots expose only registered Markdown; tools cannot read arbitrary paths or instructions/state', async (t) => {
  const f = await fixture(t); const snapshots = await buildSnapshots(validateJob(f.job)); const reader = createNoteReader(snapshots);
  assert.deepEqual(snapshots.catalog.map((note) => `${note.agent}/${note.path}`), [
    'planning/memory/index.md', 'planning/memory/topic.md', 'hyatt/Projects/README.md', 'hyatt/Projects/hotel.md',
  ]);
  assert.equal(reader.readNote({ agent: 'hyatt', path: 'Projects/hotel.md' }).sha256, sha256(await readFile(f.peer.workspace + '/Projects/hotel.md')));
  for (const relative of ['../hyatt/private.md', 'memory/../private.md', '/etc/passwd.md', 'memory\\topic.md', 'AGENTS.md', 'memory/AGENTS.md', 'state/state.md', 'instructions/secret.md']) {
    assert.throws(() => reader.readNote({ agent: 'planning', path: relative }));
  }
  for (const relative of ['private.md', 'Projects/hotel.md']) assert.throws(() => reader.readNote({ agent: 'planning', path: relative }));
  assert.equal(snapshots.canCreate('planning', 'memory/new.md'), true);
  assert.equal(snapshots.canCreate('planning', 'memory/newdir/new.md'), false);
  assert.equal(snapshots.canCreate('planning', 'other/new.md'), false);
  assert.equal(snapshots.canCreate('planning', 'memory/TOPIC.md'), false);
  assert.throws(() => snapshots.canCreate('hyatt', 'Projects/new.md'));
  assert.ok(Object.isFrozen(snapshots)); assert.ok(Object.isFrozen(snapshots.get('planning', 'memory/topic.md')));
  await writeFile(f.owner.workspace + '/memory/topic.md', 'CHANGED_AFTER_SNAPSHOT');
  assert.equal(reader.readNote({ agent: 'planning', path: 'memory/topic.md' }).content, NOTE);
  const extension = createReadExtension(loaded.sdk, reader);
  const failure = await extension.tools[1].execute({ agent: 'planning', path: '/etc/passwd.md' });
  assert.equal(failure.isError, true); assert.doesNotMatch(JSON.stringify(failure), /etc|passwd/);
});

test('configured allowlisted single Markdown files work without widening to their siblings', async (t) => {
  const f = await fixture(t);
  f.job.owner.owned_paths = ['memory/index.md', 'memory/topic.md'];
  const snapshots = await buildSnapshots(validateJob(f.job));
  assert.equal(snapshots.canCreate('planning', 'memory/new.md'), false); assert.equal(snapshots.catalog.filter((note) => note.agent === 'planning').length, 2);
});

test('symlink files, symlink directories/ancestors, hard links and nonregular note paths fail closed', async (t) => {
  const mutations = [
    async (f) => symlink(path.join(f.root, 'outside.md'), f.owner.workspace + '/memory/escape.md'),
    async (f) => symlink(f.peer.workspace + '/Projects', f.owner.workspace + '/memory/peer-dir'),
    async (f) => link(f.peer.workspace + '/Projects/hotel.md', f.owner.workspace + '/memory/hard.md'),
    async (f) => { await symlink(f.owner.workspace, f.root + '/alias'); f.job.owner.workspace = f.root + '/alias'; },
    async (f) => { await mkdir(f.owner.workspace + '/memory/not-a-file.md'); f.job.owner.indexes = ['memory/not-a-file.md']; },
    async (f) => { await rm(f.owner.workspace + '/memory/AGENTS.md'); await symlink(f.peer.workspace + '/Projects/hotel.md', f.owner.workspace + '/memory/AGENTS.md'); },
    async (f) => {
      const created = await subprocess(['-B', '-c', 'import os,sys; os.mkfifo(sys.argv[1])', f.owner.workspace + '/memory/fifo.md'], '', '/usr/bin/python3');
      assert.equal(created.code, 0, created.stderr);
    },
  ];
  for (const mutate of mutations) {
    const f = await fixture(t); await writeFile(f.root + '/outside.md', 'FOREIGN'); await mutate(f);
    await assert.rejects(buildSnapshots(validateJob(f.job)));
    const done = await run(f); assert.equal(done.result.status, 'blocked'); assert.equal(done.provider.state.callCount, 0);
  }
  const f = await fixture(t); f.job.peers[0].workspace = f.owner.workspace; f.job.peers[0].owned_paths = ['memory']; f.job.peers[0].indexes = ['memory/index.md'];
  await assert.rejects(buildSnapshots(validateJob(f.job)), /overlapping_ownership/);
});

test('full replacement requires all original pages, including noncontiguous gaps; hash from listing is insufficient', async (t) => {
  const f = await fixture(t); const long = NOTE + 'unique retained history\n'.repeat(1000);
  await writeFile(f.owner.workspace + '/memory/topic.md', long);
  const snapshots = await buildSnapshots(validateJob(f.job)); const reader = createNoteReader(snapshots);
  const note = snapshots.get('planning', 'memory/topic.md'); const proposal = JSON.stringify({ ...noChange(), edits: [editFor(note.path, long + '\nMore.\n', note.sha256)] });
  reader.listNotes({ agent: 'planning', query: 'topic' });
  assert.throws(() => validateProposal(proposal, f.job, snapshots, reader), /replacement_before_full_read/);
  reader.readNote({ agent: 'planning', path: note.path, offset: 0, limit: NOTE_PAGE_CHARS });
  reader.readNote({ agent: 'planning', path: note.path, offset: NOTE_PAGE_CHARS + 1 });
  assert.equal(reader.fullyRead('planning', note.path), false);
  assert.throws(() => validateProposal(proposal, f.job, snapshots, reader), /replacement_before_full_read/);
  let offset = 0;
  do { const p = reader.readNote({ agent: 'planning', path: note.path, offset }); offset = p.next_offset; } while (offset !== null);
  assert.equal(reader.fullyRead('planning', note.path), true);
  assert.equal(validateProposal(proposal, f.job, snapshots, reader).edits[0].content, long + '\nMore.\n');
});

test('native truncated-index replacement is rejected even if original hash is known', async (t) => {
  const f = await fixture(t); const long = NOTE + 'unread index tail\n'.repeat(1000);
  await writeFile(f.owner.workspace + '/memory/index.md', long);
  const done = await run(f, [answer({ ...noChange(), edits: [editFor('memory/index.md', NOTE, sha256(Buffer.from(long)))] })]);
  assert.equal(done.result.status, 'blocked'); assert.match(done.result.summary, /replacement_before_full_read/); assert.equal(done.closed, true);
});

test('malformed, unknown-field, foreign, instruction/state, wrong-hash and metadata-free proposals are rejected', async (t) => {
  const f = await fixture(t); const snapshots = await buildSnapshots(validateJob(f.job)); const reader = createNoteReader(snapshots);
  reader.readNote({ agent: 'planning', path: 'memory/topic.md' });
  const invalid = [
    '{"status":', '```json\n{}\n```', JSON.stringify({ ...noChange(), coverage: [] }), JSON.stringify({ ...noChange(), version: 1 }),
    JSON.stringify({ ...noChange(), extra: true }), JSON.stringify({ ...noChange(), edits: [{ ...editFor(), agent: 'hyatt' }] }),
    JSON.stringify({ ...noChange(), edits: [editFor('../hyatt/Projects/hotel.md')] }),
    JSON.stringify({ ...noChange(), edits: [editFor('Projects/hotel.md')] }),
    JSON.stringify({ ...noChange(), edits: [editFor('memory/AGENTS.md')] }),
    JSON.stringify({ ...noChange(), edits: [editFor('state/pretend.md')] }),
    JSON.stringify({ ...noChange(), edits: [editFor('memory/topic.md', NOTE, '0'.repeat(64))] }),
    JSON.stringify({ ...noChange(), edits: [editFor('memory/new.md', '# No metadata')] }),
    JSON.stringify({ ...noChange(), edits: [editFor('memory/new.md', NOTE + '\ud800')] }),
    JSON.stringify({ ...noChange(), edits: [editFor(), editFor()] }),
    JSON.stringify({ ...noChange(), status: 'blocked', edits: [editFor()] }),
    JSON.stringify({ ...noChange(), cross_links: [{ destination_agent: 'hyatt', destination_path: 'Projects/hotel.md', target_agent: 'planning', target_path: 'memory/topic.md', content: 'FOREIGN_WRITE' }] }),
    JSON.stringify({ ...noChange(), cross_links: [{ destination_agent: 'hyatt', destination_path: 'private.md', target_agent: 'planning', target_path: 'memory/topic.md' }] }),
  ];
  for (const text of invalid) assert.throws(() => validateProposal(text, f.job, snapshots, reader), undefined, text);
  for (const text of [invalid[0], invalid[2], invalid[6], invalid[10]]) {
    const done = await run(f, [answer(text)]); assert.equal(done.result.status, 'blocked'); assert.equal(done.closed, true);
    assert.deepEqual(done.result.edits, []); assert.deepEqual(done.result.cross_links, []);
  }
});

test('a model-declared blocked result is schema checked and keeps trusted coverage, no edits', async (t) => {
  const f = await fixture(t); const done = await run(f, [answer({ status: 'blocked', edits: [], cross_links: [], summary: 'Evidence conflicts; owner clarification required.' })]);
  assert.equal(done.result.status, 'blocked'); assert.match(done.result.summary, /Evidence conflicts/); assert.equal(done.result.coverage[0].processed_through_entry, 7); assert.equal(done.closed, true);
});

test('provider errors and non-stop/truncated finals are sanitized, no native retry multiplication', async (t) => {
  const f = await fixture(t);
  for (const message of [fauxAssistantMessage('', { stopReason: 'error', errorMessage: 'SECRET_PROVIDER_TOKEN=do-not-print' }),
    fauxAssistantMessage(JSON.stringify(noChange()), { stopReason: 'length' }), fauxAssistantMessage('', { stopReason: 'aborted' })]) {
    const done = await run(f, [message, answer(noChange())]);
    assert.equal(done.result.status, 'blocked'); assert.equal(done.provider.state.callCount, 1); assert.equal(done.closed, true);
    assert.doesNotMatch(JSON.stringify(done.result), /SECRET|TOKEN|do-not-print/);
  }
  const thrown = await run(f, [() => { throw new Error('SECRET_THROWN_PROVIDER'); }]);
  assert.equal(thrown.result.status, 'blocked'); assert.doesNotMatch(JSON.stringify(thrown.result), /SECRET/); assert.equal(thrown.closed, true);
});

test('cancellation aborts native work and joins Harness/storage cleanup before returning JSON', async (t) => {
  const f = await fixture(t); const controller = new AbortController();
  const done = await run(f, [(_context, options) => new Promise((resolve) => {
    options.signal.addEventListener('abort', () => resolve(fauxAssistantMessage('', { stopReason: 'aborted' })), { once: true });
    controller.abort(new Error('SECRET_CANCEL_REASON'));
  })], { signal: controller.signal });
  assert.equal(done.result.status, 'blocked'); assert.match(done.result.summary, /cancelled/); assert.equal(done.closed, true);
  assert.doesNotMatch(JSON.stringify(done.result), /SECRET/);
});

test('cancellation while stdin is waiting and while the history helper is running is joined and sanitized', async (t) => {
  const f = await fixture(t); const config = path.join(f.root, 'runtime.json'); await writeFile(config, JSON.stringify(runtime));
  const inputController = new AbortController(); let output = ''; let loadedCalls = 0;
  const stdin = new Readable({ read() { inputController.abort(new Error('SECRET_INPUT_CANCEL')); } });
  await main(['--runtime', config], { stdin, signal: inputController.signal, stdout: { write: (text) => { output += text; } },
    loadRuntime: async () => { loadedCalls++; return loaded; } });
  assert.equal(JSON.parse(output).status, 'blocked'); assert.match(output, /cancelled/); assert.doesNotMatch(output, /SECRET/); assert.equal(loadedCalls, 0);
  const helperController = new AbortController();
  await writeFile(f.job.history_helper, 'import time\ntime.sleep(60)\n');
  const cancel = setTimeout(() => helperController.abort(), 50);
  try {
    const done = await run(f, [], { helperCall: undefined, signal: helperController.signal });
    assert.equal(done.result.status, 'blocked'); assert.match(done.result.summary, /cancelled/); assert.equal(done.provider.state.callCount, 0);
  } finally { clearTimeout(cancel); }
});

test('cleanup failure blocks an otherwise valid proposal and does not leak cleanup exceptions', async (t) => {
  const f = await fixture(t); let closed = false;
  const done = await run(f, [answer(noChange())], { openHarness: async (...args) => {
    const harness = await loaded.sdk.Harness.open(...args);
    return new Proxy(harness, { get(target, key) {
      if (key === 'close') return async (...closeArgs) => { await target.close(...closeArgs); closed = true; throw new Error('SECRET_CLEANUP'); };
      const value = Reflect.get(target, key, target); return typeof value === 'function' ? value.bind(target) : value;
    } });
  } });
  assert.equal(done.result.status, 'blocked'); assert.equal(closed, true); assert.match(done.result.summary, /native_cleanup_failed/); assert.doesNotMatch(JSON.stringify(done.result), /SECRET/);
});

test('read-only credentials delegate read/list but NEVER invoke modify/delete or refresh callbacks', async () => {
  const calls = []; let callback = 0;
  const delegate = { read: async (...args) => { calls.push(['read', ...args]); return undefined; }, list: async (...args) => { calls.push(['list', ...args]); return []; },
    modify: async (_provider, fn) => { calls.push(['modify']); return fn(); }, delete: async () => { calls.push(['delete']); } };
  const credentials = readOnlyCredentials(delegate); const options = { signal: new AbortController().signal };
  assert.equal(await credentials.read('openai', options), undefined); assert.deepEqual(await credentials.list(options), []);
  await assert.rejects(credentials.modify('openai', async () => { callback++; }), /credential_writes_disabled/);
  await assert.rejects(credentials.delete('openai'), /credential_writes_disabled/);
  assert.equal(callback, 0); assert.deepEqual(calls.map((call) => call[0]), ['read', 'list']);
  assert.equal(calls[0][2], options); assert.equal(Object.hasOwn(credentials, 'login'), false); assert.equal(Object.hasOwn(credentials, 'logout'), false);
  assert.equal(Object.isFrozen(credentials), true);
});

test('real pinned auth catalog has no OpenAI API-key fallback, and expired faux OAuth cannot refresh', async () => {
  const previous = process.env.OPENAI_API_KEY; process.env.OPENAI_API_KEY = 'OFFLINE_SENTINEL_NOT_A_REAL_KEY';
  try {
    const models = loaded.auth.chatModels(readOnlyCredentials({ read: async () => undefined, list: async () => [] }));
    for (const id of ['openai', 'openai-codex']) {
      assert.equal(models.getProvider(id).auth.apiKey, undefined); assert.equal(await models.getAuth(id), undefined);
    }
  } finally { if (previous === undefined) delete process.env.OPENAI_API_KEY; else process.env.OPENAI_API_KEY = previous; }
  let refreshed = 0; let stored = 0;
  const credential = { type: 'oauth', access: 'FAKE_ACCESS', refresh: 'FAKE_REFRESH', expires: 0 };
  const store = readOnlyCredentials({ read: async () => credential, list: async () => [], modify: async () => { stored++; } });
  const fake = fauxProvider();
  const models = createModels({ credentials: store });
  models.setProvider({ ...fake.provider, auth: { oauth: { name: 'Offline faux OAuth', isSubscription: true,
    login: async () => { throw new Error('NO_LOGIN'); }, refresh: async () => { refreshed++; return credential; },
    toAuth: async () => ({ apiKey: 'FAKE_ONLY' }) } } });
  await assert.rejects(models.getAuth('faux'), (error) => error.code === 'auth' && error.cause?.message === 'credential_writes_disabled');
  assert.equal(refreshed, 0); assert.equal(stored, 0);
  let adapter; let logins = 0; let logouts = 0;
  const sentinelAuth = { SubscriptionCredentials: class { constructor() { adapter = this; } read() {} list() {} },
    chatModels: (wrapped) => {
      assert.notEqual(wrapped, adapter);
      return { ...wrapped, login: async () => { logins++; }, logout: async () => { logouts++; } };
    } };
  const restricted = subscriptionModels({ auth: sentinelAuth });
  assert.equal(typeof restricted.modify, 'function');
  await assert.rejects(restricted.login('faux', 'oauth', {}), /authentication_changes_disabled/);
  await assert.rejects(restricted.logout('faux'), /authentication_changes_disabled/);
  assert.equal(logins, 0); assert.equal(logouts, 0);
});

test('explicit refresh authorization remains credential-free for --check and idle capture', async (t) => {
  const approved = { ...runtime, allow_auth_refresh: true };
  assert.equal(validateRuntime(approved).allow_auth_refresh, true);
  assert.equal(validateRuntime(runtime).allow_auth_refresh, false);
  const f = await fixture(t, { after: 7, through: 7 });
  const config = path.join(f.root, 'approved-runtime.json'); await writeFile(config, JSON.stringify(approved));
  const cli = await subprocess([fileURLToPath(new URL('../run_review.mjs', import.meta.url)), '--runtime', config, '--check']);
  assert.equal(cli.code, 0); assert.equal(cli.stderr, ''); assert.equal(parseJson(cli.stdout).status, 'complete');
  let constructed = 0; let models = 0; let output = '';
  const approvedLoaded = { ...loaded, runtime: validateRuntime(approved), auth: {
    SubscriptionCredentials: class { constructor() { constructed++; throw new Error('MUST_NOT_READ_CREDENTIALS'); } },
    chatModels() { models++; throw new Error('MUST_NOT_CREATE_MODELS'); },
  } };
  await main(['--runtime', config, '--check'], { loadRuntime: async () => approvedLoaded,
    stdout: { write: (text) => { output += text; } } });
  assert.equal(parseJson(output).status, 'complete');
  const idle = await runReview(f.job, approved, { loadRuntime: async () => approvedLoaded, helperCall: f.helperCall });
  assert.equal(idle.status, 'complete'); assert.match(idle.summary, /no model called/);
  assert.equal(constructed, 0); assert.equal(models, 0);
});

test('authorized refresh is selected-provider and existing-OAuth only; login/logout/delete stay blocked', async () => {
  const provider = 'openai-codex'; const initial = { type: 'oauth', access: 'FAKE_A', refresh: 'FAKE_R', expires: 0 };
  let current = clone(initial); let adapter; let wrapped; let modifications = 0; let writes = 0; let deleted = 0; let login = 0;
  const calls = []; const options = { signal: new AbortController().signal };
  const auth = { SubscriptionCredentials: class {
    constructor() { adapter = this; }
    async read(...args) { calls.push(['read', ...args]); return current; }
    async list(...args) { calls.push(['list', ...args]); return [{ providerId: provider, type: 'oauth' }]; }
    async modify(id, fn, received) {
      modifications++; assert.equal(id, provider); assert.equal(received, options);
      const next = await fn(clone(current));
      if (next !== undefined) { current = clone(next); writes++; }
      return current;
    }
    async delete() { deleted++; }
  }, chatModels(store) { wrapped = store; return { credentials: store, login: async () => { login++; }, logout: async () => { deleted++; } }; } };
  const models = subscriptionModels({ auth, runtime: { ...runtime, model: { provider, modelId: 'unused' }, allow_auth_refresh: true } });
  assert.notEqual(wrapped, adapter); assert.equal(Object.isFrozen(wrapped), true);
  assert.equal(Object.hasOwn(wrapped, 'login'), false); assert.equal(Object.hasOwn(wrapped, 'logout'), false);
  await wrapped.read(provider, options); await wrapped.list(options);
  assert.equal(calls[0][2], options); assert.equal(calls[1][1], options);
  await assert.rejects(wrapped.modify('openai', () => { throw new Error('WRONG_PROVIDER_CALLBACK'); }, options), /credential_refresh_scope/);
  assert.equal(modifications, 0);
  const refreshed = { ...initial, access: 'FAKE_NEW', refresh: 'FAKE_ROTATED', expires: Date.now() + 3600000 };
  assert.deepEqual(await wrapped.modify(provider, async (value) => { assert.deepEqual(value, initial); return refreshed; }, options), refreshed);
  assert.equal(writes, 1);
  await wrapped.modify(provider, async () => undefined, options); assert.equal(writes, 1);
  await assert.rejects(wrapped.modify(provider, async () => ({ type: 'api_key', key: 'FAKE_KEY' }), options), /oauth_refresh_required/);
  let callbacks = 0;
  for (const missing of [undefined, { type: 'api_key', key: 'FAKE_KEY' }]) {
    current = missing;
    await assert.rejects(wrapped.modify(provider, async () => { callbacks++; return refreshed; }, options), /existing_oauth_required/);
  }
  assert.equal(callbacks, 0); assert.equal(writes, 1);
  await assert.rejects(wrapped.delete(provider, options), /credential_writes_disabled/);
  await assert.rejects(models.login(provider, 'oauth', {}), /authentication_changes_disabled/);
  await assert.rejects(models.logout(provider), /authentication_changes_disabled/);
  assert.equal(login, 0); assert.equal(deleted, 0);
});

async function temporaryRefresh(t, oauth) {
  const f = await fixture(t); const file = path.join(f.root, 'auth.json');
  const initial = { deviceId: '00000000-0000-4000-8000-000000000001',
    'openai-codex': { type: 'oauth', access: 'FAKE_OLD', refresh: 'FAKE_REFRESH', expires: 0 },
    openai: { type: 'oauth', access: 'FAKE_OTHER', refresh: 'FAKE_OTHER_REFRESH', expires: 0 } };
  await writeFile(file, JSON.stringify(initial) + '\n', { mode: 0o600, flag: 'wx' });
  const tracking = { reads: 0, modifications: 0, writes: 0, adapters: [] };
  const auth = { ...loaded.auth, SubscriptionCredentials: class extends loaded.auth.SubscriptionCredentials {
    constructor() { super(file); tracking.adapters.push(this); }
    async read(...args) { const value = await super.read(...args); tracking.onRead?.(++tracking.reads); return value; }
    async modify(provider, fn, options) {
      tracking.modifications++; let changed = false;
      const value = await super.modify(provider, async (current) => {
        const next = await fn(current); changed = next !== undefined; return next;
      }, options);
      if (changed) { tracking.writes++; tracking.onPersist?.(); }
      return value;
    }
  } };
  const approved = { ...runtime, model: { provider: 'openai-codex', modelId: 'unused' }, allow_auth_refresh: true };
  return { file, initial, tracking, models() {
    const models = subscriptionModels({ auth, runtime: approved });
    const provider = models.getProvider('openai-codex'); models.setProvider({ ...provider, auth: { oauth } });
    return models;
  } };
}

test('authorized refresh uses the real pinned shared-store lock once across concurrent clients and preserves failures', async (t) => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  let began; const started = new Promise((resolve) => { began = resolve; }); let refreshes = 0; let fail = false;
  const f = await temporaryRefresh(t, { name: 'OFFLINE OAuth', isSubscription: true,
    login: async () => { throw new Error('NO_LOGIN'); },
    refresh: async (current) => {
      if (fail) throw new Error('PRIVATE_REFRESH_FAILURE');
      refreshes++; began(); await gate;
      return { ...current, access: 'FAKE_NEW', refresh: 'FAKE_ROTATED', expires: Date.now() + 3600000 };
    }, toAuth: async (current) => ({ apiKey: current.access }),
  });
  const firstModels = f.models(); const secondModels = f.models();
  let secondRead; const readBoth = new Promise((resolve) => { secondRead = resolve; });
  f.tracking.onRead = (count) => { if (count === 2) secondRead(); };
  const first = firstModels.getAuth('openai-codex');
  assert.equal(await Promise.race([started.then(() => true), first.then(() => false)]), true);
  const second = secondModels.getAuth('openai-codex'); await readBoth; release();
  const results = await Promise.all([first, second]);
  assert.equal(results[0].auth.apiKey, 'FAKE_NEW'); assert.equal(results[1].auth.apiKey, 'FAKE_NEW');
  assert.equal(refreshes, 1); assert.equal(f.tracking.writes, 1); assert.equal(f.tracking.modifications, 2);
  assert.equal(f.tracking.adapters.length, 2); assert.ok(f.tracking.adapters.every((adapter) => adapter.file === f.file));
  const stored = JSON.parse(await readFile(f.file)); assert.equal(stored['openai-codex'].refresh, 'FAKE_ROTATED');
  assert.deepEqual(stored.openai, f.initial.openai); assert.equal(stored.deviceId, f.initial.deviceId);
  assert.equal((await stat(f.file)).mode & 0o777, 0o600); assert.equal((await stat(path.dirname(f.file))).mode & 0o777, 0o700);
  const before = await readFile(f.file); fail = true;
  await assert.rejects(firstModels.getAuth('openai-codex', { minOAuthValidityMs: 7200000 }), (error) => error.code === 'oauth');
  assert.deepEqual(await readFile(f.file), before); assert.equal(f.tracking.writes, 1);
  assert.equal(firstModels.getProvider('openai-codex').auth.apiKey, undefined);
});

test('authorized refresh persists a rotated token after caller cancellation without creating a second store', async (t) => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  let began; const started = new Promise((resolve) => { began = resolve; });
  let persisted; const saved = new Promise((resolve) => { persisted = resolve; });
  const controller = new AbortController();
  const f = await temporaryRefresh(t, { name: 'OFFLINE OAuth', isSubscription: true,
    login: async () => { throw new Error('NO_LOGIN'); },
    refresh: async (current, signal) => {
      assert.notEqual(signal, controller.signal); began(); await gate;
      return { ...current, access: 'FAKE_AFTER_CANCEL', refresh: 'FAKE_ROTATED_AFTER_CANCEL', expires: Date.now() + 3600000 };
    }, toAuth: async (current) => ({ apiKey: current.access }),
  });
  f.tracking.onPersist = persisted;
  const pending = f.models().getAuth('openai-codex', { signal: controller.signal });
  assert.equal(await Promise.race([started.then(() => true), pending.then(() => false)]), true);
  const rejected = assert.rejects(pending); controller.abort(new Error('FIXTURE_CANCELLED')); await rejected;
  assert.equal(f.tracking.writes, 0); release(); await saved;
  const stored = JSON.parse(await readFile(f.file));
  assert.equal(stored['openai-codex'].refresh, 'FAKE_ROTATED_AFTER_CANCEL'); assert.deepEqual(stored.openai, f.initial.openai);
  assert.equal(f.tracking.writes, 1); assert.equal(f.tracking.adapters[0].file, f.file);
});

test('cancelled native reviewer joins in-flight credential persistence before returning a result', async (t) => {
  const f = await fixture(t); const controller = new AbortController();
  let release; const gate = new Promise((resolve) => { release = resolve; }); t.after(() => release());
  let began; const started = new Promise((resolve) => { began = resolve; });
  let closed; const storageClosed = new Promise((resolve) => { closed = resolve; });
  let persisted = 0; let returned = false;
  let current = { type: 'oauth', access: 'FAKE_OLD', refresh: 'FAKE_OLD_REFRESH', expires: 0 };
  const provider = fauxProvider({ tokensPerSecond: 0 }); provider.setResponses([answer(noChange())]);
  const auth = { SubscriptionCredentials: class {
    async read() { return clone(current); }
    async list() { return [{ providerId: 'faux', type: 'oauth' }]; }
    async modify(id, fn) {
      assert.equal(id, 'faux'); const next = await fn(clone(current));
      if (next !== undefined) { current = next; persisted++; }
      return clone(current);
    }
    async delete() { throw new Error('NO_DELETE'); }
  }, chatModels(store) {
    const models = createModels({ credentials: store });
    models.setProvider({ ...provider.provider, auth: { oauth: { name: 'OFFLINE OAuth', isSubscription: true,
      login: async () => { throw new Error('NO_LOGIN'); },
      refresh: async (value) => { began(); await gate; return { ...value, refresh: 'FAKE_ROTATED', expires: Date.now() + 3600000 }; },
      toAuth: async () => ({ apiKey: 'FAKE_ONLY' }),
    } } });
    return models;
  } };
  const approved = { ...runtime, allow_auth_refresh: true };
  const pending = runReview(f.job, approved, { signal: controller.signal, helperCall: f.helperCall,
    loadRuntime: async (input) => ({ ...loaded, runtime: validateRuntime(input), auth }),
    openHarness: async (...args) => { const harness = await loaded.sdk.Harness.open(...args); harness.closed.then(closed); return harness; },
  });
  pending.then(() => { returned = true; });
  assert.equal(await Promise.race([started.then(() => true), pending.then(() => false)]), true);
  controller.abort(new Error('FIXTURE_CANCELLED')); await storageClosed;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(returned, false, 'Worker must not emit output/release ownership while an OAuth rotation is pending');
  assert.equal(persisted, 0); release();
  const result = await pending;
  assert.equal(result.status, 'blocked'); assert.match(result.summary, /cancelled/);
  assert.equal(persisted, 1); assert.equal(current.refresh, 'FAKE_ROTATED'); assert.equal(provider.state.callCount, 0);
});
