import { TOPICS } from './db.js';

const EMBED_COLOR = 0x0f62fe; // IBM blue
const OPTION_STRING = 3;
const EPHEMERAL = 1 << 6;

export const GLOSSARY = {
  'z/os': 'IBM\'s flagship 64-bit operating system for IBM Z mainframes.',
  jcl: 'Job Control Language - tells z/OS which programs a batch job runs and which datasets they use.',
  jes2: 'Job Entry Subsystem 2 - accepts jobs, schedules them and manages their output on the spool.',
  cobol: 'Common Business-Oriented Language - still runs much of the world\'s core banking, insurance and government workloads.',
  cics: 'Customer Information Control System - IBM\'s transaction server for high-volume online transactions.',
  db2: 'IBM\'s relational database; Db2 for z/OS is the mainframe edition.',
  ims: 'Information Management System - IBM\'s hierarchical database and transaction manager.',
  racf: 'Resource Access Control Facility - controls who can access which resources on z/OS.',
  tso: 'Time Sharing Option - the interactive command environment on z/OS.',
  ispf: 'Interactive System Productivity Facility - the menu panels and editor most people use on top of TSO.',
  vsam: 'Virtual Storage Access Method - access method for keyed, relative and sequential datasets.',
  dataset: 'The z/OS word for a file. Common types are sequential (PS) and partitioned (PDS/PDSE, like a folder of members).',
  lpar: 'Logical partition - a slice of a machine\'s CPUs, memory and I/O running its own operating system.',
  sysplex: 'Parallel Sysplex - a cluster of z/OS systems sharing data through a coupling facility.',
  ziip: 'IBM Z Integrated Information Processor - a specialty engine for eligible work (e.g. Java) that does not add to general-purpose software costs.',
  uss: 'z/OS UNIX System Services - the POSIX environment inside z/OS.',
  rexx: 'Restructured Extended Executor - the go-to scripting language on z/OS.',
  hlasm: 'High Level Assembler - IBM Z assembly language.',
  dasd: 'Direct Access Storage Device - mainframe-speak for disk storage.',
  smf: 'System Management Facilities - records system and job activity for accounting, performance and audit.',
  abend: 'Abnormal end - a program or task failure, identified by a code such as S0C7 (bad packed decimal data) or S322 (CPU time exceeded).',
  linuxone: 'IBM\'s Linux-only mainframe line, using the s390x architecture.',
  zowe: 'Open Mainframe Project framework with a modern CLI, REST APIs and VS Code extension for z/OS.',
};

const lookupKey = (term) => term.trim().toLowerCase().replace(/^zos$/, 'z/os');

export function lookupGlossary(term) {
  const key = lookupKey(term);
  return GLOSSARY[key] ? { term: key, definition: GLOSSARY[key] } : null;
}

export const COMMANDS = [
  {
    name: 'resources',
    description: 'List IBM Z learning resources from the hub',
    options: [{
      type: OPTION_STRING,
      name: 'topic',
      description: 'Filter by topic',
      required: false,
      choices: Object.entries(TOPICS).map(([value, name]) => ({ name, value })),
    }],
  },
  { name: 'events', description: 'Show upcoming mainframe events' },
  {
    name: 'glossary',
    description: 'Explain a mainframe term (JCL, CICS, LPAR, ...)',
    options: [{ type: OPTION_STRING, name: 'term', description: 'The term to explain', required: true }],
  },
  { name: 'hub', description: 'Links to the IBM Z hub' },
];

export function resourceEmbed(resource) {
  return {
    title: resource.title,
    url: resource.url,
    description: resource.description || undefined,
    color: EMBED_COLOR,
    footer: { text: TOPICS[resource.topic] ?? resource.topic },
  };
}

export function eventEmbed(event) {
  const unix = Math.floor(Date.parse(event.starts_at) / 1000);
  return {
    title: event.title,
    url: event.url || undefined,
    color: EMBED_COLOR,
    // <t:...:F> renders in each reader's own timezone
    description: [`<t:${unix}:F> (<t:${unix}:R>)`, event.location, event.description].filter(Boolean).join('\n'),
  };
}

const reply = (data) => ({ type: 4, data });
const ephemeral = (content) => reply({ content, flags: EPHEMERAL });

// `store` exposes listResources(topic) and upcomingEvents(limit)
export function handleCommand(interaction, store, config) {
  const { name, options = [] } = interaction.data;
  const option = (key) => options.find((o) => o.name === key)?.value;

  switch (name) {
    case 'resources': {
      const topic = option('topic');
      const resources = store.listResources({ topic, limit: 10 });
      if (!resources.length) return ephemeral(`No resources${topic ? ` for ${TOPICS[topic]}` : ''} yet.`);
      return reply({ embeds: resources.map(resourceEmbed) });
    }
    case 'events': {
      const events = store.upcomingEvents(5);
      if (!events.length) return ephemeral('No upcoming events yet.');
      return reply({ embeds: events.map(eventEmbed) });
    }
    case 'glossary': {
      const entry = lookupGlossary(option('term') ?? '');
      if (!entry) {
        return ephemeral(`I don't know that one yet. Try: ${Object.keys(GLOSSARY).join(', ')}`);
      }
      return reply({ embeds: [{ title: entry.term.toUpperCase(), description: entry.definition, color: EMBED_COLOR }] });
    }
    case 'hub':
      return reply({
        embeds: [{
          title: 'IBM Z & Mainframes hub',
          url: config.publicUrl,
          color: EMBED_COLOR,
          description: [
            `API: ${config.publicUrl}/api/resources`,
            `Events: ${config.publicUrl}/api/events`,
            config.discord.inviteUrl && `Invite friends: ${config.discord.inviteUrl}`,
          ].filter(Boolean).join('\n'),
        }],
      });
    default:
      return ephemeral('Unknown command.');
  }
}
