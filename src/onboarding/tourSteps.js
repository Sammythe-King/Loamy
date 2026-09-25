/*
 * Tour content for the Loamy dashboard.
 *
 * Every step targets a stable `data-tour` attribute rendered by Dashboard.jsx
 * (never class names or DOM position, which change with styling/refactors).
 * Steps are ordered to follow the visual flow of the page: nav → overview
 * cards → money in/out → invoices → AI → review queue → done.
 */

/**
 * @typedef {Object} LoamyTourStep
 * @property {string} element        CSS selector — always a data-tour attribute.
 * @property {string} popoverTitle
 * @property {string} popoverDescription
 * @property {"top"|"right"|"bottom"|"left"} [side]
 */

/** @type {LoamyTourStep[]} */
export const TOUR_STEPS = [
  {
    element: '[data-tour="greeting"]',
    popoverTitle: "Welcome to Loamy 👋",
    popoverDescription:
      "This is your financial command centre. Let's take a quick 60-second tour so you know exactly where everything lives.",
    side: "bottom",
  },
  {
    element: '[data-tour="nav"]',
    popoverTitle: "Navigation",
    popoverDescription:
      "Use these buttons to jump between Gmail sync, your savings Goals, and the Loamy AI chat. The red button logs you out.",
    side: "bottom",
  },
  {
    element: '[data-tour="cashflow"]',
    popoverTitle: "Cash Flow Overview",
    popoverDescription:
      "Your money at a glance: Cash In, Cash Out, and your current balance — pulled live from your connected bank alerts.",
    side: "bottom",
  },
  {
    element: '[data-tour="runway"]',
    popoverTitle: "Business Runway",
    popoverDescription:
      "Loamy estimates how many days your cash will last at your current spending rate. Watch this number to stay ahead.",
    side: "bottom",
  },
  {
    element: '[data-tour="expense-breakdown"]',
    popoverTitle: "Expense Breakdown",
    popoverDescription:
      "See exactly where your money goes each period, split by category. The biggest slice is called out below the chart.",
    side: "top",
  },
  {
    element: '[data-tour="invoices"]',
    popoverTitle: "Recent Invoices",
    popoverDescription:
      "Track who owes you and what you've been paid. Create and manage invoices from the Invoices page.",
    side: "top",
  },
  {
    element: '[data-tour="quick-actions"]',
    popoverTitle: "Ask Loamy AI",
    popoverDescription:
      'Have a question about your numbers? Tap "Ask Loamy AI" to chat with your finance assistant, or "Refresh Data" to pull the latest from Gmail.',
    side: "top",
  },
  {
    element: '[data-tour="needs-review"]',
    popoverTitle: "Needs Review",
    popoverDescription:
      "When a bank transfer can't be auto-categorised, it lands here. One tap on a category keeps your spending reports accurate.",
    side: "top",
  },
  {
    element: '[data-tour="greeting"]',
    popoverTitle: "You're all set! 🎉",
    popoverDescription:
      "That's the tour — you know your way around now. Connect Gmail, log an invoice, and let Loamy watch the leaks. Happy saving!",
    side: "bottom",
  },
];