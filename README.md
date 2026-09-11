# TATA

TATA (TATA-Assisted Teaching Assistant) is a grading helper for teaching
assistants. It pulls student submissions from Canvas, grades each one against
a rubric (your scoring standard) you write, and produces a score summary you
can review.

Do you know coding, Python, uv, or the Canvas API? You do not need to. TATA
was built for TAs, not programmers. If you can open a web browser and type
two commands into a terminal, you are ready.

## What you need before starting

1. **A Canvas account** for the course you want to grade. TATA accesses
   student submissions with your own login, so use the same account you use
   to view student work.

2. **A language model (LLM) provider.** TATA sends each submission to an
   LLM to grade it. Ollama is the recommended choice: a free local LLM
   server, no account or API key needed. Install it once (`brew install ollama` on macOS, or the installer from the Ollama website), start it
   with `ollama serve`, and pull the bundled model with
   `ollama pull qwen3.8:latest`. Prefer a cloud provider? Any
   OpenAI-compatible service works (DeepSeek, for example), but then you
   also need an API key. See [docs/config/provider.md](docs/config/provider.md)
   for other options.

3. **A Windows, macOS, or Linux computer.** Nothing else, no special
   hardware or accounts.

## Install (about 3 minutes)

TATA is managed by [uv](https://docs.astral.sh/uv/), a tool that installs
Python and every dependency for you. You never install or configure Python
yourself.

1. Install uv, one time only.

   Windows (open PowerShell, paste this):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   macOS / Linux:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Restart your terminal (close and reopen it) so the `uv` command becomes
   available.

2. Get the TATA project files and go into the project folder. The
   repository reaches you through your course's internal channel: clone it
   from your internal source, or download the project files and extract
   them. Then open a terminal in the project folder:

   ```bash
   cd path/where/you/saved/TATA
   ```

3. Install everything:

   ```bash
   uv sync
   ```

   This single command downloads Python, installs all libraries TATA needs,
   and prepares the project. The first run takes a few minutes. After that,
   instead of "uv sync" you can simply start TATA (next section).

## Get your Canvas API token

TATA talks to Canvas using a token issued to you by Canvas.

1. Sign in to your Canvas site in a browser.
2. Click your avatar picture (top right corner), then click **Settings**.
3. Scroll down until you see **API Access Token**.
4. Click the button to generate a new token (it may say "New Access Token").
   Canvas then shows a long string. Copy it.
5. Save it somewhere private (a text editor or a password manager).

The token is shown only once. If you lose it, delete the old one and
generate a fresh one.

Your **base URL** is the Canvas address you see in the browser address bar
while signed in, for example `https://your-school.instructure.com`. It is
the exact site address, nothing after the course part.

## Quick Start (recommended: graphical interface)

From the project folder, start the workbench:

```bash
uv run tui
```

The window has two tabs at the top: Dashboard and Library. Click a tab to
switch (or press Tab / Shift+Tab). Settings opens as a fullscreen view from
the Dashboard: press `,` or click the `⚙ Settings` button in the top action
row (it works at every dashboard level, and closes with `escape`). Press
`ctrl+p` anywhere to open the command palette and fuzzy-search the current
view's actions (for example `gra` to run Grade). Follow these six steps:

1. **Project keys.** Open Settings with `,` (or the `⚙ Settings` button),
   then the Canvas sub-tab. Fill in your Canvas URL and your token, click
   **Save .env** (a plain text file
   at the project root that stores your keys; Configuration below explains
   it), then click **Test Canvas connection**. TATA reports whether the
   connection works.
   (Inside Settings, keys 1 to 4 switch between Grading, Canvas,
   Plagiarism, and Paths / Advanced.)

2. **Tell TATA which LLM to use.** Go to the Library tab, then the Providers
   sub-tab. The bundled `ollama` provider is already listed and works as
   is: base_url `http://localhost:11434/v1`, model `qwen3.8:latest`, no
   API key needed (start Ollama with `ollama serve` first). Using a cloud
   provider instead? Create a new provider: give it a name, set base_url
   and model to match the service, pick a mode (for example `tool_call`),
   and paste your key into api_key. Alternatively, write
   `${DEEPSEEK_API_KEY}` in the api_key field and put the real key into the
   project's `.env` file; the bundled example provider `deepseek`
   does exactly that.

3. **Pick a rubric.** In the Library tab, Rubrics sub-tab, create a new
   rubric, or start from the bundled `example_rubric` to experiment. A
   rubric is your scoring standard (see Concepts below).

4. **Bring your course in.** Switch to the Dashboard tab and press `c`.
   Pick your course from the list. TATA creates a course config (a
   configuration file) and remembers the course id. Now press `c` again on
   the course row to import an assignment: choose the assignment, then pick
   the rubric, prompt (your grading instruction), and provider to use, and
   click Import. TATA writes the assignment config and downloads the
   submissions right away. (Other Dashboard keys: `r` rescan, `F` fetch all,
   `s` score review.)

5. **Run the pipeline.** Select the assignment to open its work area, then
   press `p` (preprocess), `g` (grade), and `s` (score), one after another.
   Watch the log at the bottom for progress. Press `x` to stop a running
   job (queued items are dropped, the in-flight item finishes) and `e` to
   edit the config.

6. **Review the scores.** Go back to the course row in the Dashboard and
   press `s` to open the score review for the assignment.

That is the whole loop. Every assignment you import gets its own folder and
config. `fetch`, `preprocess` and `grade` keep content-hash caches
(`.cache/`), so rerunning them skips unchanged work. `score` and `analyze`
simply recompute when rerun, which is safe.

## Command line (optional)

If you like typing, the CLI runs the exact same engine as the workbench.
The stage order is always: `preprocess` then (optionally) `plagiarism`,
then `grade`, then `score`, then `analyze`.

```bash
uv run cli fetch -c data/<course>/config.toml
uv run cli validate -c data/<course>/<assignment>/config.toml
uv run cli preprocess -c data/<course>/<assignment>/config.toml
uv run cli plagiarism -c data/<course>/<assignment>/config.toml
uv run cli grade -c data/<course>/<assignment>/config.toml
uv run cli score -c data/<course>/<assignment>/config.toml
uv run cli analyze -c data/<course>/<assignment>/config.toml
```

`fetch` is the odd one out: it reads the course config instead of an
assignment config, and pulls every assignment the course config lists. To
fetch a single assignment, append its Canvas ids:
`uv run cli fetch -c data/<course>/config.toml <course_id> <assignment_id>`.
Fetch talks to Canvas, so it needs the token from `.env` (see Get your
Canvas API token above).

Other useful subcommands:

- `view`: open the score review for a graded assignment. The argument is
  the assignment's `graded` folder, which holds one JSON file per student:
  `uv run cli view data/<course>/<assignment>/graded`. Add `--web` after
  the path to open the same view in a browser instead of the TUI.
- `config set`: change one value of a config, for example
  `uv run cli config set -c data/my-assignment/config.toml grading.max_parallel_tasks 4`.
- `rubric generate`: write a draft rubric from the fetched assignment
  description.

Run `uv run cli --help` any time to see what is available.

## Concepts in one line each

- **Course**: one Canvas course (for example "CSCI 101"), stored as a
  folder under `data/<course>/`.
- **Assignment**: one graded activity inside a course, stored as
  `data/<course>/<assignment>/`, named by its Canvas id.
- **Rubric**: your scoring standard: criteria, points per criterion, and
  rating levels, written as a TOML file under `data/rubrics/`.
- **Prompt**: the instructions you give the LLM about how to grade,
  markdown files under `data/prompt/`.
- **Provider**: the LLM service to call, described by one TOML file under
  `data/providers/` containing base_url, api_key, model, and mode.
- **Config**: a TOML text file describing an assignment: which rubric,
  prompt, provider, and folders to use. See Configuration below.
- **Raw and processed**: `raw/` holds submissions as they come from
  Canvas; `preprocess` converts each student's files into clean markdown
  in `processed/` for grading.
- **Score**: the stage that turns per-student grading results into a
  readable score summary (written under `scored/`).
- **Plagiarism**: optional detection of similar code or text between
  submissions (can be run per assignment or aggregated over a course).

## Configuration (layers & paths)

Config files are written in TOML, a plain text format of `key = value`
lines and `[section]` headers. Settings live in three layers, and closer
layers win:

1. Global `data/config.toml`: defaults shared across courses (for example
   the course id in `[fetch]`).
2. Course `data/<course>/config.toml`: the course id and the course's
   assignment list.
3. Assignment `data/<course>/<assignment>/config.toml`: everything grading
   related for that assignment.

Only `[grading]` is required in an assignment config, and it needs three
keys:

```toml
[grading]
rubric = "rubrics/example_rubric.toml"
system_prompt = "prompt/system.md"
provider = "ollama"
```

The repository bundles two providers: `ollama` (local, recommended, no
key) and `deepseek` (a cloud alternative that reads
`DEEPSEEK_API_KEY` from `.env`).

How paths resolve: `rubric` and `system_prompt` are relative to the
`data/` folder (for example `data/rubrics/example_rubric.toml`);
`reference_file`, if set, is relative to the assignment folder; `provider`
names a file `data/providers/<name>.toml`.

Secrets live in a `.env` file at the project root, which TATA loads
automatically. It looks like this:

```env
DEEPSEEK_API_KEY=your-deepseek-key
CANVAS_BASE_URL=https://your-school.instructure.com
CANVAS_ACCESS_TOKEN=your-canvas-token
```

`DEEPSEEK_API_KEY` is optional: add it only if you use a key-based provider
such as DeepSeek (the bundled Ollama provider needs no key).
`FIRECRAWL_API_KEY` is optional too: add it only if your submissions
include scanned images that need OCR.

## Documentation

- Onboarding, step by step: [docs/onboarding.md](docs/onboarding.md)
- Frequently asked questions: [docs/faq.md](docs/faq.md)
- Common problems and fixes: [docs/troubleshooting.md](docs/troubleshooting.md)
- Assignment config format: [docs/config/assignment.md](docs/config/assignment.md)
- Provider config format: [docs/config/provider.md](docs/config/provider.md)
- Rubric config format: [docs/config/rubric.md](docs/config/rubric.md)

## One-sentence prompt for coding agents

The block below sums up TATA for a coding agent. Paste it as the first
message and the agent can drive the whole workflow for you: run the CLI,
run every stage, and report back instead of reading the code.

```markdown
You are working on TATA, a grading helper for teaching assistants that pulls
student submissions from Canvas and grades each against a rubric with an
LLM; the repository organizes everything under data/, with config.toml in
three layers: global data/config.toml, course data/<course>/config.toml
([fetch] course id and assignment list), and assignment
data/<course>/<assignment>/config.toml ([grading]: rubric, system_prompt,
provider); a fresh checkout ships only data/example until you configure a
course. All operations go through the CLI, so start with `uv run cli
--help`. Set up `.env` with `cp .env.sample .env`, filling
CANVAS_BASE_URL and CANVAS_ACCESS_TOKEN (add an API key only if you use a
cloud provider; the bundled local Ollama provider needs none, but run
`ollama pull qwen3.8:latest` and keep `ollama serve` running, since validate
does not check LLM connectivity). Pull
submissions with `uv run cli fetch -c data/<course>/config.toml` (append
course and assignment ids to fetch one assignment), then run each stage with
`uv run cli <stage> -c data/<course>/<assignment>/config.toml` in this
order: validate, preprocess, plagiarism (optional), grade, score, analyze.
Validation, preprocessing, plagiarism, scoring, and analysis can run
offline, except scanned pages (preprocess uses hosted OCR) and plagiarism's
first run (downloads its embedding model); grade needs the LLM (local Ollama
or a keyed cloud provider); fetch needs Canvas. Help me drive this CLI end to
end: run each stage, summarize
what it printed, and when a choice is ambiguous (course, assignment,
provider, rubric, or any config value), ask me and wait for my answer
instead of guessing; never take an irreversible decision without confirming
with me.
```

## Starter assets

The repository ships with working example files:

- `data/example/config.toml`: an example assignment config
- `data/example/alias.toml`: example display names for course, assignment,
  and students
- `data/rubrics/example_rubric.toml`: an example rubric
- `data/prompt/system.md`: a generic grading prompt
- `data/prompt/lab.md`: a lab-specific grading addendum, listed alongside
  `system.md` in `system_prompt` to combine them
- `data/providers/ollama.toml`: the bundled local LLM provider, the
  recommended default (no key needed)
- `data/providers/deepseek.toml`: an optional cloud provider that
  reads `DEEPSEEK_API_KEY` from your `.env` file

Once you are set up, confirm everything is ready with:

```bash
uv run cli validate -c data/example/config.toml
```

If it finishes without error, TATA is ready for your first real assignment.
