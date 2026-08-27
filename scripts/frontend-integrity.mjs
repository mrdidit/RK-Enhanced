import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const indexPath = "dist/index.js";
const manifestPath = "dist/frontend-integrity.json";
const prefix = "rke-frontend-sha256-v1:";
const placeholder = `${prefix}${"0".repeat(64)}`;
const markerPattern = /rke-frontend-sha256-v1:([0-9a-f]{64})/g;
const sha256 = value => createHash("sha256").update(value).digest("hex");

function matches(buffer) {
  return [...buffer.toString("utf8").matchAll(markerPattern)];
}

function stamp() {
  const original = readFileSync(indexPath);
  const found = matches(original);
  if (found.length !== 1 || found[0][0] !== placeholder) {
    throw new Error("frontend integrity placeholder must occur exactly once");
  }
  const bundleDigest = sha256(original);
  const stamped = Buffer.from(
    original.toString("utf8").replace(placeholder, `${prefix}${bundleDigest}`),
    "utf8",
  );
  const manifest = {
    protocol: 1,
    algorithm: "sha256-normalized-v1",
    bundle_id: `${prefix}${bundleDigest}`,
    index_sha256: sha256(stamped),
  };
  writeFileSync(indexPath, stamped);
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
}

function verify() {
  const stamped = readFileSync(indexPath);
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const found = matches(stamped);
  if (found.length !== 1) throw new Error("frontend integrity marker must occur exactly once");
  const bundleId = found[0][0];
  const normalized = Buffer.from(
    stamped.toString("utf8").replace(bundleId, placeholder),
    "utf8",
  );
  if (manifest.protocol !== 1 || manifest.algorithm !== "sha256-normalized-v1" ||
      manifest.bundle_id !== bundleId || manifest.index_sha256 !== sha256(stamped) ||
      found[0][1] !== sha256(normalized)) {
    throw new Error("frontend integrity verification failed");
  }
}

const action = process.argv[2];
if (action === "stamp") stamp();
else if (action === "verify") verify();
else throw new Error("usage: frontend-integrity.mjs <stamp|verify>");
