# GitHub Secrets Configuration

## PYPI_API_TOKEN

Required for the `publish.yml` workflow to publish bathos to PyPI.

### Setup Steps

1. Generate a PyPI API token:
   - Go to https://pypi.org/manage/account/token/
   - Click "Add API token"
   - Scope: Entire account
   - Copy the generated token (starts with `pypi-...`)

2. Add the secret to GitHub:
   ```bash
   # Using GitHub CLI (requires authentication)
   gh secret set PYPI_API_TOKEN --body "$(pbpaste)"  # macOS
   gh secret set PYPI_API_TOKEN --body "$(xclip -o)" # Linux
   
   # Or via GitHub web UI:
   # 1. Go to https://github.com/mariellerossi/bathos/settings/secrets/actions
   # 2. Click "New repository secret"
   # 3. Name: PYPI_API_TOKEN
   # 4. Value: paste the token
   ```

3. Verify:
   ```bash
   gh secret list
   ```

### Usage

The `publish.yml` workflow uses this token to authenticate with PyPI:
- Triggered on tag push (`v*`) or manual dispatch
- Builds the distribution with `uv build`
- Publishes with `uv publish --token ${{ secrets.PYPI_API_TOKEN }}`

### Security

- Tokens are masked in workflow logs
- Only visible to maintainers with admin access
- Rotate if exposed

### Testing

To test the publish workflow without releasing:
1. Create a test token for TestPyPI
2. Add a separate `TEST_PYPI_TOKEN` secret
3. Add an optional workflow for test releases

See `publish.yml` for current implementation.
