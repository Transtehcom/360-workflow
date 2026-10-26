// Проверяем порядок сохранения и восстановление после сбоя, а не детали DOM.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { DraftSaver } = require('../web/app.js');
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
