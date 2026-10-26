// Проверяем порядок сохранения и восстановление после сбоя, а не детали DOM.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { DraftSaver, generatePassword, PASSWORD_ALPHABET, canRetry, fetchOptions } = require('../web/app.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('медленный запрос не затирает более новый ответ; одновременно работает один запрос', async () => {
  const calls = [];
  const releases = [];
  const saver = new DraftSaver((key, value) => { calls.push([key, value]); return new Promise(resolve => releases.push(resolve)); }, () => {});
  saver.change('A1', 1, true);
  await tick();
  saver.change('A1', 5, true);
  saver.change('A2', 4, true);
  assert.deepEqual(calls, [['A1', 1]]);
  releases.shift()(); await tick();
  assert.deepEqual(calls, [['A1', 1], ['A1', 5]]);
  releases.shift()(); await tick();
  assert.deepEqual(calls.at(-1), ['A2', 4]);
  releases.shift()(); await tick();
  assert.equal(saver.pending, false);
});

test('ошибка входа сохраняет изменения для повторной отправки после входа', async () => {
  let connected = false;
  const calls = [];
  const saver = new DraftSaver(async (key, value) => {
    if (!connected) { const error = new Error('Требуется вход'); error.status = 401; throw error; }
    calls.push([key, value]);
  }, () => {});
  saver.change('c:A', 'Первый текст');
  assert.equal(await saver.flush(), false);
  assert.equal(saver.error.status, 401);
  assert.equal(saver.pending, true);
  saver.change('c:A', 'Уточнённый текст');
  connected = true;
  assert.equal(await saver.flush(), true);
  assert.deepEqual(calls, [['c:A', 'Уточнённый текст']]);
  assert.equal(saver.error, null);
  assert.equal(saver.pending, false);
});

test('переход с поля отправляет последний текст до окончания задержки', async () => {
  const calls = [];
  const saver = new DraftSaver(async (key, value) => calls.push([key, value]), () => {}, 60000);
  saver.change('O1', 'а');
  saver.change('O1', 'аб');
  assert.equal(saver.pending, true);
  assert.equal(await saver.flush(), true);
  assert.deepEqual(calls, [['O1', 'аб']]);
  assert.equal(saver.timer, null);
});

test('сбой в середине очереди не повторяет уже сохранённые ответы', async () => {
  const calls = [];
  let failed = false;
  const saver = new DraftSaver(async (key, value) => {
    if (key === 'A2' && !failed) { failed = true; throw new Error('Нет связи'); }
    calls.push([key, value]);
  }, () => {});
  saver.change('A1', 3); saver.change('A2', 4); saver.change('extra', 'Спасибо');
  assert.equal(await saver.flush(), false);
  assert.deepEqual(calls, [['A1', 3]]);
  assert.equal(await saver.flush(), true);
  assert.deepEqual(calls, [['A1', 3], ['A2', 4], ['extra', 'Спасибо']]);
});

for (const status of [409, 404]) {
  test(`постоянная ошибка ${status} не оставляет очередь в ожидании и не блокирует переход`, async () => {
    const calls = [];
    const saver = new DraftSaver(async (key, value) => {
      calls.push([key, value]);
      const error = new Error('Анкета закрыта'); error.status = status; throw error;
    }, () => {});
    saver.change('A1', 3); saver.change('A2', 4);
    assert.equal(await saver.flush(), true);
    assert.equal(saver.pending, false);
    assert.equal(saver.error, null);
    assert.equal(saver.closed.status, status);
    assert.deepEqual(calls, [['A1', 3]]);
    // Очередь остановлена: новые изменения не уходят на сервер и не создают ожидание.
    saver.change('A3', 5, true);
    assert.equal(await saver.flush(), true);
    assert.equal(saver.pending, false);
    assert.deepEqual(calls, [['A1', 3]]);
  });
}

test('временная ошибка 500 по-прежнему оставляет запись для повтора', async () => {
  const saver = new DraftSaver(async () => { const error = new Error('Сбой'); error.status = 500; throw error; }, () => {});
  saver.change('A1', 3);
  assert.equal(await saver.flush(), false);
  assert.equal(saver.pending, true);
  assert.equal(saver.closed, null);
});

test('пароль для участника: 10 читаемых символов без похожих 0/O/o и 1/l/I', () => {
  for (let i = 0; i < 200; i++) {
    const password = generatePassword();
    assert.equal(password.length, 10);
    assert.match(password, /^[a-zA-Z2-9]{10}$/);
    assert.doesNotMatch(password, /[0O1lIo]/);
  }
  // Каждый символ алфавита достижим: значения случайного источника отображаются на весь алфавит.
  const all = generatePassword(PASSWORD_ALPHABET.length, n => Uint32Array.from({ length: n }, (_, i) => i));
  assert.equal(all, PASSWORD_ALPHABET);
  assert.equal(new Set(PASSWORD_ALPHABET).size, PASSWORD_ALPHABET.length);
});

test('POST уходит с keepalive и типом JSON: сохранение при закрытии вкладки не обрывается', () => {
  const signal = new AbortController().signal;
  const post = fetchOptions({ key: 'O1', value: 'текст' }, signal);
  assert.equal(post.method, 'POST');
  assert.equal(post.keepalive, true);
  assert.deepEqual(post.headers, { 'Content-Type': 'application/json' });
  assert.equal(post.body, JSON.stringify({ key: 'O1', value: 'текст' }));
  assert.equal(post.signal, signal);
  assert.equal(post.credentials, 'same-origin');
  assert.equal(post.cache, 'no-store');
  // пустое тело (выход, отправка анкеты) — тоже JSON-тип
  assert.deepEqual(fetchOptions({}, signal).headers, { 'Content-Type': 'application/json' });
  const get = fetchOptions(undefined, signal);
  assert.equal(get.method, 'GET');
  assert.equal(get.body, undefined);
  assert.notEqual(get.keepalive, true);
  assert.deepEqual(get.headers, {});
  // предел keepalive — 64 КБ: самый длинный ответ (5000 символов кириллицей) далеко не доходит
  assert.ok(Buffer.byteLength(fetchOptions({ key: 'extra', value: 'я'.repeat(5000) }, signal).body) < 64 * 1024);
});

test('«Повторить»: после закрытия оценки — только для поставленной задачи, которая не обновилась', () => {
  for (const status of ['ошибка Битрикс24: сбой', 'задача не поставлена', 'задача не обновилась в Битрикс24']) {
    assert.equal(canRetry(status, true, true), true, status);
    assert.equal(canRetry(status, true, false), false, status);  // Битрикс24 не подключён
  }
  assert.equal(canRetry('задача не обновилась в Битрикс24', false, true), true);
  assert.equal(canRetry('ошибка Битрикс24: сбой', false, true), true);
  assert.equal(canRetry('задача не поставлена', false, true), false);
  for (const status of ['задача поставлена', 'ставится…', 'не связан с Битрикс24']) {
    assert.equal(canRetry(status, true, true), false, status);
    assert.equal(canRetry(status, false, true), false, status);
  }
});
