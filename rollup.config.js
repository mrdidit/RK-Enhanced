import decky from "@decky/rollup";

// ROCKNIX currently ships a Decky frontend which evaluates plugin bundles as
// classic scripts. @decky/rollup defaults to ESM, whose trailing `export`
// causes `Unexpected token: export` before the plugin can initialize.
const config = decky();
config.output = { ...config.output, format: "iife" };

export default config;
