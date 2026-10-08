# Getting started (Windows)

This guide takes you from the zip file to a project that runs on GitHub, and optionally on your own laptop.

1. [Install the tools](#1-install-the-tools)
2. [Put the project on GitHub](#2-put-the-project-on-github)
3. [Watch it run on GitHub Actions](#3-watch-it-run-on-github-actions)
4. [Run it on your laptop](#4-run-it-on-your-laptop) (or [in a Codespace](#5-or-run-it-in-github-codespaces))
5. [Save your results](#6-save-your-results)

Run every command in **PowerShell**.

## 1. Install the tools

For steps 2 and 3 (GitHub only) you need just **Git**. For running it on your laptop you need everything below.

| Tool | Install | Check it works |
| --- | --- | --- |
| Git | git-scm.com, default options | `git --version` |
| Python 3.10 or newer | python.org; tick **"Add python.exe to PATH"** | `python --version` |
| Docker Desktop | docker.com; keep the default **WSL 2** option | `docker info` |
| kubectl | Comes with Docker Desktop. If missing: `winget install -e --id Kubernetes.kubectl` | `kubectl version --client` |
| k3d | See below | `k3d version` |

**Installing k3d**, either:

- With Chocolatey: `choco install k3d`, or
- By hand: download `k3d-windows-amd64.exe` from the releases page at github.com/k3d-io/k3d/releases, rename it to `k3d.exe`, put it in a folder such as `C:\tools`, and add that folder to your PATH (Start > "Edit environment variables for your account" > Path > New). Open a new PowerShell window afterwards.

**If your laptop has less than 8 GB of RAM**, limit how much memory Docker can take. Create a file called `.wslconfig` in your user folder (`C:\Users\<you>\.wslconfig`) containing:

```
[wsl2]
memory=3GB
processors=2
```

Then run `wsl --shutdown` and start Docker Desktop again.

## 2. Put the project on GitHub

1. Unzip the project, for example to `C:\Users\<you>\Projects\self-healing-payments-api`. Check that `README.md` sits directly inside that folder, not inside another folder of the same name.
2. Open `README.md` and replace `YOUR-GITHUB-USERNAME` in the badge on line 3 with your GitHub username.
3. On github.com, click **New repository**:
   - Name: `self-healing-payments-api`
   - Visibility: **Public** (so interviewers can see it; Actions minutes are free for public repos)
   - Leave "Add a README", ".gitignore" and "license" **unticked**.
4. In PowerShell:

```powershell
cd C:\Users\<you>\Projects\self-healing-payments-api

# only the first time you use Git on this laptop
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

git init
git add .
git commit -m "Payments API with Kubernetes manifests, failure drills and CI"
git branch -M main
git remote add origin https://github.com/<your-username>/self-healing-payments-api.git
git push -u origin main
```

A browser window may open the first time and ask you to sign in to GitHub. After the push, refresh the repository page: you should see the files and the README.

## 3. Watch it run on GitHub Actions

Pushing to `main` starts the pipeline automatically.

1. Open the **Actions** tab of your repository and click the newest **CI** run.
2. Three jobs run one after another:
   - **Unit tests**: 27 tests, 5 of them against a real PostgreSQL database.
   - **Build image and validate manifests**: builds the Docker image and checks every Kubernetes file.
   - **Deploy to Kubernetes and run failure drills**: creates a real k3d cluster on GitHub's machine, deploys everything, runs all five drills, then deletes the cluster. This job takes the longest, roughly 10 to 15 minutes.
3. When it finishes, scroll down the run's **Summary** page to see the drill results. The full table is saved as the **drill-results** artifact at the bottom of the page.
4. To run it again without pushing: Actions > CI > **Run workflow**.

A green tick next to the commit means everything passed, and the badge in the README turns green.

If a job fails, open it and read the red step. The last step of the drills job prints the pod list, recent Kubernetes events and API logs to help you find out why.

## 4. Run it on your laptop

Make sure Docker Desktop is running, then from the project folder:

```powershell
python scripts/cluster.py up
```

The first run downloads the k3s, PostgreSQL, Prometheus and Python images, so it can take 5 to 10 minutes. It ends with `All set. Try it:`.

Try the API:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8080/payments -ContentType "application/json" -Body '{"amount": "25.50", "currency": "SGD", "description": "coffee"}'
Invoke-RestMethod http://localhost:8080/payments
```

Look at what is running:

```powershell
python scripts/cluster.py status
kubectl get pods -n payments --watch      # Ctrl+C to stop
```

Run the drills, one at a time or all together:

```powershell
python scripts/drills.py pod-crash
python scripts/drills.py hang
python scripts/drills.py bad-release
python scripts/drills.py traffic-spike
python scripts/drills.py db-restart
python scripts/drills.py all
```

Watch it in a second PowerShell window while a drill runs:

```powershell
kubectl get pods,hpa -n payments --watch
```

Open Prometheus in a third window:

```powershell
kubectl port-forward -n payments svc/prometheus 9090:9090
```

Then go to http://localhost:9090 and try the queries in the README. After the hang drill, check the **Alerts** tab.

When you are done, free the memory:

```powershell
python scripts/cluster.py down
```

**On a laptop with less than 8 GB of RAM**: close other programs first, and run the drills one at a time. If `traffic-spike` makes the laptop struggle, skip it; GitHub Actions runs it for you.

## 5. Or run it in GitHub Codespaces

A Codespace is a computer in the cloud that you use from your browser. It is useful when your laptop is short on memory.

1. On your repository page, click **Code > Codespaces > Create codespace on main**.
2. Wait for the setup to finish (it installs Docker, kubectl and k3d for you).
3. In the terminal at the bottom, run the same commands as in step 4 (use `curl` instead of `Invoke-RestMethod`).
4. Stop the Codespace when you are done (Code > Codespaces > ... > Stop) so it does not use up your free monthly hours.

## 6. Save your results

Each drill run adds rows to `drills/results.md`. Once you have a run you are happy with:

1. Copy its rows into the **My results** section of `README.md`.
2. Copy `docs/postmortem-template.md` to `docs/postmortem.md` and fill it in for one drill, in your own words.
3. Commit and push:

```powershell
git add README.md drills/results.md docs/postmortem.md
git commit -m "Add failure drill results and postmortem"
git push
```

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `Docker is not running` | Start Docker Desktop and wait until it says "Engine running". |
| `Missing tools: k3d` | k3d is not on your PATH. Reinstall it (section 1) and open a new PowerShell window. |
| `port is already allocated` on `cluster.py up` | Something else uses port 8080. Close it, or run `python scripts/cluster.py down` if an old cluster is still there. |
| Pods stuck in `Pending` | Not enough memory. Run `kubectl describe pod <name> -n payments` and read "Events"; close other programs or raise the `.wslconfig` memory. |
| A pod in `CrashLoopBackOff` | `kubectl logs <pod-name> -n payments --previous` shows why it crashed. |
| `http://localhost:8080/payments` gives 404 | The ingress is still starting. Wait a minute and try again. |
| `traffic-spike` shows no scale-up | The autoscaler needs about a minute of CPU data. Run it again with `--duration 180`. |
| `git push` is rejected | The GitHub repository was created with a README. Delete and recreate it without one, or run `git pull --rebase origin main` first. |
