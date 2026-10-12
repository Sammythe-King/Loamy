const BANK_DOMAINS = [
  { match: ["first bank", "firstbank", "firstalert", "fbn"], domain: "firstbanknigeria.com" },
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

export const resolveBankDomain = (...names) => {
  const haystack = names.filter(Boolean).join(" ").toLowerCase();
  if (!haystack) return null;
  const hit = BANK_DOMAINS.find(({ match }) => match.some((key) => haystack.includes(key)));
  return hit ? hit.domain : null;
};

export const googleFaviconUrl = (domain) =>
  `https://s2.googleusercontent.com/s2/favicons?domain=${domain}&sz=128`;

// Ordered fallbacks; the BankLogo component advances on load errors and on
// Google's 16px placeholder globe, which it returns with a 200 for unknown domains.
export const resolveBankLogoSources = (...names) => {
  const domain = resolveBankDomain(...names);
  if (!domain) return [];
  return [googleFaviconUrl(domain), `https://icons.duckduckgo.com/ip3/${domain}.ico`];
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
