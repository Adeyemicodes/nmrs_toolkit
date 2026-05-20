# NMRS Toolkit — Facility User Guide

A simple guide to using the NMRS Toolkit at your facility. You don't need any
technical background — just follow the steps below.

---

## What this app does

The NMRS Toolkit helps you work with data from your facility's NMRS (Nigeria
Medical Record System) database. It can:

- Generate line list reports
- Combine several reports into one
- Automatically keep safe, encrypted backups of your database

Your IT manager has already set it up for your facility, so you can start using
it right away.

---

## Starting the app

Double-click the **NMRSToolkit** icon (or the program file your manager pointed
you to).

If a password box appears, type the password your manager gave you and click
**LOGIN**. If no password box appears, that's fine — the app simply opens
straight away.

---

## The tabs

Across the top of the app you'll see a few tabs. Here's what each one is for:

| Tab | What it does |
|-----|--------------|
| **Linelists** | Pick a report, run it, and save the result. This is your main day-to-day tool. |
| **Merge Reports** | Combine two or more saved reports into a single file. |
| **Backup** | See backup status and make a backup right now if you want to. |
| **Restore** | Put a saved backup back into the database (only when needed — see below). |

At the bottom is an **Activity Log** that shows what the app is doing. If
something doesn't work, this is the first place to look.

---

## Generating a line list

1. Open the **Linelists** tab.
2. Choose the report you want from the list.
3. Click the run button.
4. When it finishes, choose where to save the file.

That's it. If you tick the "Encrypt output" box, the saved file is locked and
can only be opened with the toolkit — useful when sending sensitive data.

---

## Backups happen automatically

You don't have to remember to back up. The app **backs up your database by
itself**:

- Once when the computer starts for the day, and
- Again at 2:00 PM on weekdays.

It only keeps one backup per day, and it automatically removes very old ones so
your disk doesn't fill up. Backups are **encrypted**, so even if someone copies
the file, they can't read the data without the toolkit.

### Want to make a backup right now?

1. Open the **Backup** tab.
2. Click **BACKUP NOW**.
3. Wait for it to say it's done.

### Where are the backups kept?

In a folder called **NMRS_DB** on the computer. Click **Open Folder** on the
Backup tab to see it. Leave these files alone — they are your safety net.

---

## Restoring a backup (use with care)

Restoring replaces the **current** database with the contents of a saved
backup. Only do this if your database was lost or damaged, or if your manager
asks you to.

1. Open the **Restore** tab.
2. Click **Browse...** and pick a backup file.
3. Check the database name shown is correct.
4. Click **RESTORE**.

The app automatically makes a fresh safety backup *before* it replaces
anything, and it will ask you to confirm. A progress bar shows how it's going —
large databases can take a while, so just let it finish.

> **If you're not sure, stop and call your manager first.** Restoring is the one
> action that changes your live data.

---

## If something goes wrong

1. Look at the **Activity Log** at the bottom of the app — it usually says what
   happened.
2. Note down any message you see.
3. Contact your IT manager with that message.

Your manager can fix configuration issues, recover backups, and answer
anything this guide doesn't cover.
