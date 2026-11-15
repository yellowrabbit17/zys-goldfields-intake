import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "..");

function resolvePython() {
  const candidates = [
    path.join(rootDir, "venv", "bin", "python"),
    "python3",
    "python",
  ];
  for (const bin of candidates) {
    if (bin.includes(path.sep)) {
      if (fs.existsSync(bin)) {
        return bin;
      }
    } else {
      return bin;
    }
  }
  return "python3";
}

function run(cmd, args, label) {
  console.log(`\n[build] ${label}: ${cmd} ${args.join(" ")}`);
  execFileSync(cmd, args, {
    cwd: rootDir,
    stdio: "inherit",
    env: {
      ...process.env,
      ZYS_SKIP_DOTENV: process.env.ZYS_SKIP_DOTENV || "1",
    },
  });
}

const tscBin = path.join(rootDir, "node_modules", ".bin", "tsc");
if (!fs.existsSync(tscBin)) {
  throw new Error("TypeScript binary not found at node_modules/.bin/tsc. Run npm install first.");
}
run(tscBin, ["--noEmit"], "Typechecking neon.ts");

const pythonBin = resolvePython();
const pyFiles = fs
  .readdirSync(rootDir, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".py"))
  .map((entry) => entry.name)
  .sort();

if (pyFiles.length === 0) {
  throw new Error("No Python source files found to compile.");
}
run(pythonBin, ["-m", "py_compile", ...pyFiles], `Compiling ${pyFiles.length} Python modules`);
run(pythonBin, ["intake_app.py", "--build-html"], "Generating static intake_portal.html");

console.log("\n[build] Build completed successfully.");
