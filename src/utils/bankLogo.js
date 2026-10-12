const BANK_DOMAINS = [
  { match: ["first bank", "firstbank", "firstalert", "fbn"], domain: "firstbanknigeria.com", alternates: ["firstbankgroup.com"] },
  { match: ["wema", "alat"], domain: "wemabank.com" },
  { match: ["gtbank", "gtb", "guaranty", "gt bank"], domain: "gtbank.com" },
  { match: ["zenith"], domain: "zenithbank.com" },
  { match: ["access"], domain: "accessbankplc.com" },
  { match: ["opay"], domain: "opayweb.com" },
  { match: ["uba", "united bank for africa"], domain: "ubagroup.com" },
  { match: ["kuda"], domain: "kuda.com" },
  { match: ["palmpay"], domain: "palmpay.com" },
  { match: ["moniepoint"], domain: "moniepoint.com" },
  { match: ["sterling"], domain: "sterling.ng" },
  { match: ["fidelity"], domain: "fidelitybank.ng" },
  { match: ["fcmb", "first city monument"], domain: "fcmb.com" },
  { match: ["stanbic"], domain: "stanbicibtcbank.com" },
  { match: ["union bank"], domain: "unionbankng.com" },
  { match: ["polaris"], domain: "polarisbanklimited.com" },
  { match: ["keystone"], domain: "keystonebankng.com" },
  { match: ["ecobank"], domain: "ecobank.com" },
  { match: ["standard chartered"], domain: "sc.com" },
  { match: ["providus"], domain: "providusbank.com" },
];

const findBank = (names) => {
  const haystack = names.filter(Boolean).join(" ").toLowerCase();
  if (!haystack) return null;
  return BANK_DOMAINS.find(({ match }) => match.some((key) => haystack.includes(key))) || null;
};

export const resolveBankDomain = (...names) => findBank(names)?.domain ?? null;

export const googleFaviconUrl = (domain) =>
  `https://s2.googleusercontent.com/s2/favicons?domain=${domain}&sz=128`;

// Google is the only source: it returns a 16px placeholder for domains it has
// no icon for, which BankLogo skips. DuckDuckGo is deliberately not used because
// its placeholder is a full-size grey circle that cannot be told apart from a real logo.
export const resolveBankLogoSources = (...names) => {
  const bank = findBank(names);
  if (!bank) return [];
  return [bank.domain, ...(bank.alternates || [])].map(googleFaviconUrl);
};

export const bankInitials = (name = "") =>
  String(name)
    .replace(/bank/gi, "")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join("") || "B";
