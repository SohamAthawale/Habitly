import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export const TOPICS = {
  general: 'General mainframe',
  zos: 'z/OS',
  jcl: 'JCL & batch',
  cobol: 'COBOL',
  rexx: 'REXX',
  hlasm: 'Assembler (HLASM)',
  db2: 'Db2',
  cics: 'CICS',
  ims: 'IMS',
  linuxone: 'Linux on Z / LinuxONE',
  zowe: 'Zowe & DevOps',
  security: 'Security (RACF etc.)',
  careers: 'Learning & careers',
};

const SCHEMA = `
  CREATE TABLE IF NOT EXISTS resources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    topic       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
  );
  CREATE INDEX IF NOT EXISTS idx_resources_topic ON resources(topic);

  CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    starts_at   TEXT NOT NULL,           -- ISO 8601, UTC
    url         TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
  );
  CREATE INDEX IF NOT EXISTS idx_events_starts_at ON events(starts_at);

  -- anonymous website usage counts: no cookies, IPs or user ids, just what happened and where from
  CREATE TABLE IF NOT EXISTS hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
  );
  CREATE INDEX IF NOT EXISTS idx_hits_created_at ON hits(created_at);

  -- RMIT student sign-ups from the website's join form (for club affiliation);
  -- one row per student number, re-joining updates it
  CREATE TABLE IF NOT EXISTS members (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    student_number TEXT NOT NULL UNIQUE,     -- s1234567
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,            -- ...@student.rmit.edu.au
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
  );
`;

// starter content so the hub and /resources are not empty on day one
const SEED_RESOURCES = [
  ['Open Mainframe Project', 'https://www.openmainframeproject.org/', 'general',
    'Linux Foundation project behind open-source mainframe tools, courses and mentorships.'],
  ['COBOL Programming Course', 'https://github.com/openmainframeproject/cobol-programming-course', 'cobol',
    'Free, hands-on COBOL course from the Open Mainframe Project.'],
  ['Zowe', 'https://www.zowe.org/', 'zowe',
    'Open-source CLI, APIs and VS Code tooling for working with z/OS.'],
  ['z/OS documentation', 'https://www.ibm.com/docs/en/zos', 'zos',
    'Official IBM documentation for every z/OS release.'],
  ['IBM Z Xplore', 'https://www.ibm.com/z/resources/zxplore', 'careers',
    'Free IBM learning platform with real mainframe access and badges.'],
];

export function openDatabase(path) {
  if (path !== ':memory:') {
    mkdirSync(dirname(path), { recursive: true });
  }
  const db = new DatabaseSync(path);
  db.exec('PRAGMA journal_mode = WAL;');
  db.exec(SCHEMA);

  if (db.prepare('SELECT COUNT(*) AS n FROM resources').get().n === 0) {
    const insert = db.prepare('INSERT INTO resources (title, url, topic, description) VALUES (?, ?, ?, ?)');
    for (const row of SEED_RESOURCES) insert.run(...row);
  }
  return db;
}
