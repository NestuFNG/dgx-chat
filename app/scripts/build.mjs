/**
 * 跨平台构建入口：删除 dist，然后以 production 模式调用 rspack。
 * 上游的 `rm -rf dist && NODE_ENV=production ...` 在 PowerShell / cmd 下不可用，
 * 这里用 Node 完成同样的动作，Windows、macOS、Linux 表现一致。
 */
import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dist = path.join(root, 'dist')

await rm(dist, { recursive: true, force: true })
console.log('[build] removed', path.relative(root, dist))

const require = createRequire(import.meta.url)

/** 解析 @rspack/cli 的可执行入口，避免依赖 shell 的 PATH 与 .cmd 后缀 */
const resolveRspackBin = () => {
  const packageJsonPath = require.resolve('@rspack/cli/package.json')
  const { bin } = JSON.parse(readFileSync(packageJsonPath, 'utf8'))
  const relative = typeof bin === 'string' ? bin : bin?.rspack
  if (!relative) {
    throw new Error('@rspack/cli does not expose a rspack binary')
  }
  const resolved = path.resolve(path.dirname(packageJsonPath), relative)
  if (!existsSync(resolved)) {
    throw new Error(`rspack binary not found: ${resolved}`)
  }
  return resolved
}

const rspackBin = resolveRspackBin()
const child = spawn(process.execPath, [rspackBin, 'build', '-c', 'rspack.config.ts'], {
  cwd: root,
  stdio: 'inherit',
  env: { ...process.env, NODE_ENV: 'production' },
})

child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`[build] rspack terminated by ${signal}`)
    process.exit(1)
  }
  process.exit(code ?? 1)
})
child.on('error', (error) => {
  console.error('[build] failed to start rspack:', error)
  process.exit(1)
})
