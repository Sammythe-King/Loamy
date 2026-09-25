import { useEffect } from "react";
import { driver } from "driver.js";
import "driver.js/dist/driver.css";
import "./tour.css";

import { TOUR_STEPS } from "./tourSteps";
import { getTourStatus, setTourStatus, TOUR_STATUS } from "./tourStorage";

/*
 * OnboardingTour — first-time product tour for the Dashboard, built on the
 * current Driver.js API (v1.8: driver(), drive(), onDestroyStarted, ...).
 *
 * Behaviour:
 *  - Auto-starts ONLY for a user with no stored tour status (first visit).
 *    Completion/skip is persisted per-user in localStorage (tourStorage.js),
 *    so refreshes, browser restarts and re-logins never re-trigger it.
 *  - A parent can force a replay by bumping the `replaySignal` prop
 *    (Dashboard wires this to the "Tour" nav button).
 *  - Reaching the last step and pressing Next/Finish → status "completed".
 *    Closing early (X button / ESC) → status "skipped" (replayable later).
 *  - `skipMissingElement` + `waitForElement` make the tour fail soft: a
 *    missing target is skipped after a short wait instead of crashing.
 *  - StrictMode-safe: the driver instance is created inside the effect and
 *    destroyed on cleanup, and a module-level singleton guards against two
 *    tours ever running at once.
 */

/** @type {import("driver.js").Driver | null} */
let activeDriver = null;

const destroyActiveTour = () => {
  if (activeDriver) {
    try {
      activeDriver.destroy();
    } catch {
      // destroy() is idempotent in practice; guard anyway so cleanup never throws.
    }
    activeDriver = null;
  }
};

const OnboardingTour = ({ userId, replaySignal = 0 }) => {
  useEffect(() => {
    if (!userId) return undefined;

    // First-time detection: auto-start only when this user has never finished
    // OR skipped the tour. A replay (replaySignal > 0) always starts.
    const hasSeenTour = getTourStatus(userId) !== null;
    if (hasSeenTour && replaySignal === 0) return undefined;

    // Wait one frame so the dashboard's first committed paint (cards, charts)
    // is in the DOM before we highlight anything. Dashboard also renders this
    // component only after its loading spinner resolves, so this is belt and
    // braces against chart/layout settling.
    const rafId = requestAnimationFrame(() => {
      if (activeDriver?.isActive()) return; // never run two tours at once

      const driverObj = driver({
        steps: TOUR_STEPS.map((step) => ({
          element: step.element,
          popover: {
            title: step.popoverTitle,
            description: step.popoverDescription,
            side: step.side || "bottom",
            align: "start",
          },
        })),

        // ---- Look & feel (see tour.css for the popover theme) ----
        popoverClass: "loamy-tour-popover",
        overlayColor: "#0B3D0B",
        overlayOpacity: 0.6,
        stagePadding: 8,
        stageRadius: 20,

        // ---- Behaviour ----
        animate: true,
        smoothScroll: true,
        allowClose: true,
        allowKeyboardControl: true,
        disableActiveInteraction: true,
        advanceOnClick: false,

        // Fail soft: if a target is missing (e.g. empty-state variant swapped
        // a card out), wait briefly then skip that step instead of throwing.
        skipMissingElement: true,
        waitForElement: 500,

        // ---- Buttons & progress ----
        showButtons: ["next", "previous", "close"],
        showProgress: true,
        progressText: "{{current}} of {{total}}",
        nextBtnText: "Next",
        prevBtnText: "Back",
        doneBtnText: "Finish",

        // ---- Accessibility: expose the popover as a labelled dialog ----
        onPopoverRender: (popover) => {
          popover.wrapper.setAttribute("role", "dialog");
          popover.wrapper.setAttribute("aria-label", "Product tour");
        },

        /*
         * Single exit funnel. Driver.js routes EVERY destroy request through
         * onDestroyStarted — the X button, ESC, and clicking Next on the last
         * step all land here. Destruction only actually happens when we call
         * destroy() ourselves, so this is where completion vs. skip is decided.
         */
        onDestroyStarted: () => {
          const finished = driverObj.isLastStep();
          setTourStatus(
            userId,
            finished ? TOUR_STATUS.COMPLETED : TOUR_STATUS.SKIPPED
          );
          driverObj.destroy();
        },

        onDestroyed: () => {
          if (activeDriver === driverObj) activeDriver = null;
        },
      });

      activeDriver = driverObj;
      driverObj.drive();
    });

    return () => {
      cancelAnimationFrame(rafId);
      destroyActiveTour();
    };
  }, [userId, replaySignal]);

  // Driver.js renders its overlay/popover imperatively outside the React tree,
  // so this component intentionally renders nothing.
  return null;
};

export default OnboardingTour;