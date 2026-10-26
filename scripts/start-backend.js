#!/usr/bin/env node
/**
 * Bootstraps the Python backend and hands over to uvicorn.
 *
 * - Picks a Python 3.11+ interpreter (backend/.venv, then common binaries).
 * - Creates backend/.venv and installs backend/requirements.txt on first run.
 * - Streams uvicorn logs with a stable prefix so `concurrently` output is readable.
 *
 * Env overrides: CLIPFORGE_PYTHON, CLIPFORGE_SKIP_INSTALL=1, HOST, PORT.
 */
import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BACKEND_DIR = join(ROOT, 'backend')
const VENV_DIR = join(BACKEND_DIR, '.venv')
const VENV_PYTHON = join(VENV_DIR, 'bin', 'python')
const REQUIREMENTS = join(BACKEND_DIR, 'requirements.txt')
const STAMP = join(VENV_DIR, '.requirements.sha')

const isWindows = process.platform === 'win32'
const venvPython = isWindows ? join(VENV_DIR, 'Scripts', 'python.exe') : VENV_PYTHON

function log(message) {
  process.stdout.write(`[backend] ${message}\n`)
}

function fail(message) {
  process.stderr.write(`[backend] ${message}\n`)
  process.exit(1)
}

function candidates() {
  const list = []
  if (process.env.CLIPFORGE_PYTHON) list.push(process.env.CLIPFORGE_PYTHON)
  list.push('python3.12', 'python3.13', 'python3.11', 'python3')
  return list
}

function pythonVersion(bin) {
  const result = spawnSync(bin, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'], {
    encoding: 'utf8',
  })
  if (result.status !== 0) return null
  return result.stdout.trim()
}

function findPython() {
  for (const bin of candidates()) {
    const version = pythonVersion(bin)
    if (!version) continue
    const [major, minor] = version.split('.').map(Number)
    if (major === 3 && minor >= 11) return { bin, version }
  }
  return null
}

function ensureVenv() {
  if (existsSync(venvPython)) return

  const python = findPython()
  if (!python) {
    fail(
      'Python 3.11+ not found. Install it (e.g. `brew install python@3.12`) or set CLIPFORGE_PYTHON.',
    )
  }

  log(`creating virtualenv with ${python.bin} (Python ${python.version})`)
  const created = spawnSync(python.bin, ['-m', 'venv', VENV_DIR], { stdio: 'inherit' })
  if (created.status !== 0) fail('failed to create backend/.venv')
}

function installRequirements() {
  if (process.env.CLIPFORGE_SKIP_INSTALL === '1') return
  if (!existsSync(REQUIREMENTS)) fail('backend/requirements.txt is missing')

  const hash = createHash('sha256').update(readFileSync(REQUIREMENTS)).digest('hex')

  if (existsSync(STAMP) && readFileSync(STAMP, 'utf8').trim() === hash) {
    return
  }

  log('installing Python dependencies (first run can take a few minutes)')
  const pip = spawnSync(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'], {
    stdio: 'inherit',
  })
  if (pip.status !== 0) fail('failed to upgrade pip')

  const install = spawnSync(venvPython, ['-m', 'pip', 'install', '-r', REQUIREMENTS], {
    stdio: 'inherit',
  })
  if (install.status !== 0) fail('failed to install backend/requirements.txt')

  mkdirSync(VENV_DIR, { recursive: true })
  writeFileSync(STAMP, hash)
}

ensureVenv()
installRequirements()

const host = process.env.HOST ?? '127.0.0.1'
const port = process.env.PORT ?? '8000'

log(`starting uvicorn on http://${host}:${port}`)

const child = spawn(
  venvPython,
  [
    '-m',
    'uvicorn',
    'backend.main:app',
    '--host',
    host,
    '--port',
    port,
    '--reload',
    '--reload-dir',
    'backend',
  ],
  { cwd: ROOT, stdio: 'inherit', env: process.env },
)

const shutdown = (signal) => {
  if (!child.killed) child.kill(signal)
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))

child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal)
  process.exit(code ?? 0)
})
