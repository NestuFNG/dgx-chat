/**
 * 运行单元测试：先用 tsc 把 src + tests 编译到 .test-build（Node 无法直接运行带 .js
 * 后缀导入的 TS 源码），再交给 Node 内置 test runner。整个流程零额外依赖。
 */
import { spawnSync } from 'node:child_process'
import { readdirSync, rmSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const buildDir = path.join(root, '.test-build')
const tsc = path.join(root, 'node_modules', 'typescript', 'bin', 'tsc')

/** 递归收集编译后的 *.test.js（Node 22 的 --test 不接受目录参数） */
const collectTestFiles = (dir) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      return collectTestFiles(full)
    }
    return entry.name.endsWith('.test.js') ? [full] : []
  })

rmSync(buildDir, { recursive: true, force: true })

const compile = spawnSync(process.execPath, [tsc, '-p', 'tsconfig.test.json'], {
  cwd: root,
  stdio: 'inherit',
})
if (compile.status !== 0) {
  process.exit(compile.status ?? 1)
}

const testFiles = collectTestFiles(path.join(buildDir, 'tests'))
if (testFiles.length === 0) {
  console.error('[test] no compiled test files found')
  process.exit(1)
}

const run = spawnSync(process.execPath, ['--test', ...testFiles], {
  cwd: root,
  stdio: 'inherit',
})
process.exit(run.status ?? 1)
