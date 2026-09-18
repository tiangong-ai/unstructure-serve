// Stable absolute paths allow PM2 resurrect from any working directory.
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '../..');
const files = [
  'ecosystem.config.json', 'ecosystem.two_stage.celery.json',
  'ecosystem.celery.json', 'ecosystem.vllm.parallele.config.json',
  'ecosystem.two_stage.flower.json', 'ecosystem.celery.flower.json',
];
module.exports = { apps: files.flatMap(file => {
  const apps = JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8')).apps;
  return apps.map(app => ({
    ...app, cwd: root,
    script: path.resolve(root, app.script),
    interpreter: app.interpreter === 'none' ? 'none' : path.resolve(root, app.interpreter),
    out_file: path.join(root, 'output/logs', `${app.name}.log`),
    error_file: path.join(root, 'output/logs', `${app.name}.log`),
  }));
})};
