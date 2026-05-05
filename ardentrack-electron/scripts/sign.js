"use strict";

/**
 * Custom signing script for electron-builder.
 *
 * Uses signtool.exe with a YubiKey (or other hardware-token / certificate-store cert).
 *
 * Environment variables (set before running electron-builder):
 *   WIN_SIGN_CERT_SHA1  – SHA-1 thumbprint of the code-signing certificate
 *                         (run: certutil -store My   to list installed certs)
 *   WIN_SIGN_TIMESTAMP  – (optional) RFC 3161 timestamp server URL
 *                         Default: http://timestamp.digicert.com
 *
 * The YubiKey PIN prompt will appear during signing — this is expected.
 */

const { execSync } = require("child_process");
const path = require("path");
const fs = require("fs");

function resolveSigntool() {
  const sdkPath = "C:\\Program Files (x86)\\Windows Kits\\10\\bin\\10.0.26100.0\\x64\\signtool.exe";
  if (fs.existsSync(sdkPath)) return `"${sdkPath}"`;
  return "signtool";
}

exports.default = async function sign(configuration) {
  const filePath = configuration.path;
  if (!filePath) return;

  const thumbprint = process.env.WIN_SIGN_CERT_SHA1;
  if (!thumbprint) {
    console.log("[sign] WIN_SIGN_CERT_SHA1 not set — skipping signing for", path.basename(filePath));
    return;
  }

  const tsaUrl = process.env.WIN_SIGN_TIMESTAMP || "http://timestamp.digicert.com";

  const cmd = [
    resolveSigntool(),
    "sign",
    "/sha1", thumbprint,
    "/fd", "sha256",
    "/tr", tsaUrl,
    "/td", "sha256",
    "/d", "ArdenTrack",
    `"${filePath}"`,
  ].join(" ");

  console.log("[sign]", path.basename(filePath));
  try {
    execSync(cmd, { stdio: "inherit" });
  } catch (e) {
    console.error("[sign] Signing failed for", filePath, e.message);
    throw e;
  }
};
