> 中文版：[../ADMIN.md](../ADMIN.md)

# Administrator Guide

Admin panel URL: `http://<lsa-web IP>/admin`. You can switch the interface language at the top right.

- [Basic concepts](#basic-concepts)
- [Accounts](#accounts)
- [Question bank](#question-bank)
- [Question sets (assessment/practice)](#question-sets-assessmentpractice)
- [Practice and attendance](#practice-and-attendance)
- [Assessment checklist](#assessment-checklist)
- [Score overview and participant page](#score-overview-and-participant-page)
- [Instance (participant environment) management](#instance-participant-environment-management)
- [System settings](#system-settings)
- [What participants see](#what-participants-see)
- [Using it for technical interviews](#using-it-for-technical-interviews)

---

## Basic concepts

| Term | Meaning |
|---|---|
| **Question** | A question package (see [QUESTIONS.md](QUESTIONS.md)); once built, it becomes a **template** on PVE |
| **Question set** | One assessment or one practice session: which questions, points, open window, and scoring policy. There are two types: "Assessment" and "Practice" |
| **Instance** | One participant's environment for one question in one question set (a linked clone of the template). **The same question placed in different question sets means different environments and different scores** |
| **Check** | Runs the question's check.yml to grade it, either when the participant presses "Check answer" or during "final grading" after an assessment ends |

## Accounts

Participant accounts are **issued in advance**: the administrator generates accounts and prints slips to hand out to participants, and each participant enters their ID to bind it on first login.

- **Generate accounts**: prefix + count + starting number, e.g. `p001`–`p060`, with random 8-character passwords
- **Print login slips**: several accounts per sheet; cut them apart and hand them out to participants
- **Import CSV**: `username,password[,participant_code][,note]`; an empty password is generated randomly; if an ID is given, the account is bound immediately
- **Export CSV**: includes initial passwords and binding status
- Per-row actions: **Unbind** (when a participant bound the wrong ID), **Reset password**, **Delete**

> Accounts are bound to IDs, and scores and attendance are organized by ID. It is a good idea to generate a few spare accounts.

## Question bank

- **Upload zip**: one question per zip (the zip contains the question folder). A question with the same name is replaced
- **Build template + self-test**: builds this question's template from the base template (1–5 minutes), then tests it automatically:
  an unanswered environment should score 0, and applying `solution.yml` should score full marks; "self-test passed" is shown only if both are correct
- If a question's `setup.yml` / `files/` changes, it is marked as **needing a rebuild**; changes to only the description, check.yml, or translations do not require a rebuild
- On the question page you can view the build log, preview the question, export a zip, or delete it
- The question bank page lists which question sets use each question

> Rebuilding a template does **not** affect instances already handed out to participants (they were cloned from the old template).
> To make participants use the new version, you must delete that question set's instances (they are recreated the next time a participant opens the question).

## Question sets (assessment/practice)

You can add question sets on the "Question sets (assessment/practice)" page. When adding one, you can **copy the questions and points of an existing question set**. New question sets are hidden by default.

### Question set settings

| Setting | Description |
|---|---|
| Type | **Practice**: does not count toward assessments; time can be unlimited. **Assessment**: requires a time window; environments stop automatically at the end, and final grading can be run |
| Start / end time | Practice may leave them empty (empty start = open now, empty end = open indefinitely); assessments require both |
| Visible to participants | Unchecked = draft, hidden from participants |
| Set notice | Shown to participants above the question list |
| Scoring policy | **Best score per question** / **Latest check per question** / **Only the final grading after the end** (timed questions always use the best score within the answering time) |
| Check cooldown | Minimum number of seconds between two checks |
| Participants can see whether each check item passed | When off, participants see only the total score; recommended on for practice, as needed for assessments |
| Block pasting in the terminal | Ctrl+V, right-click, and drag-and-drop are all blocked; the server side also discards and logs them |

### Per-question settings

In the question table at the bottom of the question set page: check "Include" and save, and the question appears in this question set.

| Field | Description |
|---|---|
| Points | Points for each check item (defaults come from question.yaml) |
| Time limit (min) | 0 = untimed (answer freely while the question set is open). For timed questions, a countdown starts once answering begins; when time is up, the environment is reset |
| Attempts | How many times a timed question can be started, 0 = unlimited |

### How assessments and practice interact

**While any assessment is in progress, all practice question sets are automatically paused**: practice environments stop, terminals disconnect, and participants are redirected to the assessment. Practice resumes after the assessment ends.

## Practice and attendance

Open one practice question set for each class (or each training session), and you get both **practice scores** and **attendance**:

1. Add a practice question set (e.g. "Week 5 practice") and **set its start/end time to the class period**
2. A participant who enters the question set, opens a question, connects to the terminal, or presses check within that time is recorded as **present** (merely logging in does not count)
3. Once the end time passes, participants can no longer check, so scores are fixed
4. The "**Practice summary**" lists each participant's attendance and score for every practice session; you can set a "late by more than N minutes" marker and export CSV

The attendance column in the Score overview shows the number of IPs; one person with multiple IPs may have switched computers or had someone else log in for them. The "Activity log" on the participant page has detailed times and IPs.

> After a practice session ends and scores are final, go to the question set page and press "**Delete all instances of this set**" to free space (score records are kept).
> The number of instances the system can hold is limited; see [OPERATIONS.md](OPERATIONS.md#storage-lvm-limits).

## Assessment checklist

**One week before the assessment**

- [ ] All assessment questions show "self-test passed"
- [ ] Create the assessment question set: questions, points, time, scoring policy, whether to block pasting
- [ ] Do a complete run-through with a test account (open a question, answer, check, view scores)
- [ ] Give lsa-web a static IP and change the admin password (see [INSTALL.md](INSTALL.md#7-network-setup-before-an-assessment))
- [ ] Generate and print participant accounts

**The day before the assessment**

- [ ] Delete instances of practice question sets that are no longer needed (to free space)
- [ ] Confirm that all participants have bound their IDs (or prepare to bind them on site)
- [ ] On the assessment question set page, press "**Prewarm instances**": this creates environments for all questions for accounts already bound to an ID.
      **About 8 seconds each; 60 people × 12 questions takes about 1.5 hours**, so do it early. When the assessment starts, participants only need to start them
- [ ] Keeping "Max running environments per participant" in System settings at **1** is recommended
- [ ] In the assessment notice, write: only one question can be open at a time, and switching questions pauses the previous one (files are kept); remember to `enable` any service you start manually

**During the assessment**

- "Score overview" with the assessment question set selected: see each person's per-question scores, attendance, and running environments in real time
- "**Extend 10 minutes**": extends the whole assessment
- Participant page: see that participant's instances, timed attempts (you can end now or reset attempts), check history, and terminal logs
- If a participant's environment breaks: ask the participant to press "Reset environment" themselves (it returns to the initial state), or handle it from the admin panel

**After the assessment**

- [ ] When time is up, the system automatically stops assessment environments and disconnects terminals
- [ ] If the scoring policy is "final grading", press "**Final grading (all)**": regrades based on the environment state at the end of the assessment
      (only instances that were answered are checked; 165 take about 13 minutes)
- [ ] "Export scores CSV"
- [ ] Once scores are final, delete the assessment question set's instances

## Score overview and participant page

- **Score overview**: switch question sets at the top; shows the score matrix (per person, per question), total score, attendance, and running environments; can be exported to CSV
- **Participant page** (click an ID to open it):
  - Instances: environments and status for each question in each question set
  - Timed attempts: "End now" (and reset the environment), "Reset attempts"
  - Check history: the time, score, and message of each check; click in to see each check item and the Ansible output
  - Terminal logs: grouped by question set + question; each row shows the number of sessions, total time, best score (and number of checks) and latest activity.
    Expand a row to see each terminal connection (start time, length, number of inputs, blocked pastes)
    - **Replay**: plays back like a video with asciinema-player (pause, speed, seek); idle periods longer than 2 seconds are shortened
    - On the right is the **input list** (each command line, keys such as arrows and ⌫, with times); click one to jump to it. Blocked pastes are shown in red
    - "Input text" and "Screen text" show plain text (easy to search and copy); ".cast" downloads the original file, which `asciinema play` can play locally
    - Recordings are deleted together with the account or question set
  - Activity log: logins, entering question sets, opening questions, connecting to terminals (the basis for attendance)

## Instance (participant environment) management

The "Instances" page lists all participant environments; you can stop, start, or delete them individually, or stop all of them.

- When a participant is idle (not connected to a terminal) for longer than the configured time, the environment is paused automatically, with its contents kept
- After a question set ends, its environments stop automatically
- Pausing is always a **clean shutdown** (not a power cut), so participants' files are not lost

## System settings

| Setting | Description |
|---|---|
| Site title, site notice | The notice is shown at the top of every page after participants log in |
| Max running environments per participant | **1 recommended**: opening a new question automatically pauses the previous one (contents kept). With 2 or more, memory may run short when there are many VM questions |
| Auto-pause when idle (minutes) | Environments are paused when no terminal has been connected for longer than this |
| System-wide instance limit | Checked when prewarming instances, to avoid exceeding what the storage can hold |
| Default language | Traditional Chinese / English / Follow browser language; participants and administrators can each switch on their own |
| ID format | Regular expression checked when binding an ID |

## What participants see

1. Log in (pre-issued account) → on first login, enter the ID twice to bind it
2. Home page: question sets currently open (while an assessment is in progress, it goes straight to the assessment)
3. Question set page: question list, score for each question, time remaining
4. Question page: question description on the left, terminal on the right; buttons "Check answer", "Reset environment", "Reconnect"; the terminal can be popped out into a separate window
5. The first time a question is opened, its environment is created (LXC about 10–30 seconds, VM about 1 minute); after that it only needs to be started

The account inside the participant environment is `user` (with sudo), and there is no network access.

## Using it for technical interviews

The same workflow can also be used to give job candidates hands-on troubleshooting tasks (the interface uses the neutral terms "participant / ID", and the word "student" never appears):

1. **Create accounts and bind them directly**: "Account" → "Import CSV", for example
   ```
   username,password,participant_code,note
   cand01,,C2026001,Alex Chen backend engineer
   cand02,,C2026002,Sam Lee SRE
   ```
   An empty password is generated randomly; if `participant_code` is filled in, the account is bound immediately, so the candidate does not need to enter an ID after logging in.
   The ID must match the ID format in "System settings" (default: 4–20 alphanumeric characters)
2. **Create an assessment question set**: set the open window to the interview slot (or the whole day), and add the questions you want to test
   - For each question, e.g. "Time limit" 20 minutes and "Attempts" 1
   - "Participants can see whether each check item passed" as needed: off = only the total score is shown, with no hints about what still isn't fixed
   - Enable "Block pasting in the terminal" if needed
3. **On the interview day**: give the candidate the username and password (online interviews work too, as long as they can reach the site)
4. **Review afterwards**: the "participant page" in the admin panel shows
   - **Terminal logs**: replay the candidate's whole session and jump to key commands from the input list (to see their reasoning and whether they were trying things at random)
   - **Check history**: the time of each check and the result of each item
   - Activity log: times and IPs of opening questions and connecting
5. Afterwards, delete the question set's instances; accounts can be deleted or kept for the record

Interview sessions usually have few people, so an ordinary small host (e.g. 16GB of memory) is enough.
