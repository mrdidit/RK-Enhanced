Beta.9 corrects the final user-interaction dependency in beta.8's verified
update path. Decky can now reload, validate RK-Enhanced, and finish the update
without Quick Access or the plugin panel being reopened.

## Changelog

- Moves install readiness from the Quick Access React panel to the registered
  frontend bundle's startup lifecycle.
- Requires the exact integrity-stamped bundle to execute and complete a real
  `getState()` round trip before acknowledging the candidate backend.
- Cancels the bounded readiness probe when Decky dismounts that frontend
  generation.
- Retains beta.8's nonce, bundle/backend hash, process-generation, lifecycle,
  transaction, and rollback checks.
- Continues to fetch and validate the latest stable Decky Loader during every
  install, update, and reinstall started by beta.8 or later.
- Uses descriptive GitHub release names and a visible release changelog.

## Updating

- **From beta.8:** use RK-Enhanced's normal Update action. No panel reopening is
  required after Decky reloads.
- **From beta.7 or older:** run the full installer once because those older
  updaters cannot refresh Decky as part of the first transition:

  ```sh
  curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
  ```

After beta.9 is installed, future Update and Reinstall actions refresh stable
Decky automatically.

## Validation

- 191 local regression tests pass.
- TypeScript, Python, POSIX shell, frontend-integrity, source-map, packaging,
  transaction, and rollback checks pass.
- A beta.8 to beta.9 upgrade on the Pocket FIT Elite with Decky v3.2.6 passed
  exact backend/frontend readiness without any post-reload panel interaction.

This remains a pre-release. Bugs and device-specific gaps are still expected;
reports with logs from `/storage/homebrew/logs/RK-Enhanced/` are welcome.
