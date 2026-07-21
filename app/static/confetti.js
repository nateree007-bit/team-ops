/* Blue & green confetti burst, fired when the URL carries ?celebrate=1
   (set by completing a to-do). Respects prefers-reduced-motion. */
(function () {
  const params = new URLSearchParams(window.location.search);
  if (params.get("celebrate") !== "1") return;

  // Clean the URL so refreshing doesn't replay the celebration
  params.delete("celebrate");
  const clean = window.location.pathname + (params.toString() ? "?" + params.toString() : "");
  history.replaceState(null, "", clean);

  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const COLORS = ["#0067c5", "#3ba0e2", "#63b8ee", "#00ad4d", "#4cc38a", "#7ddfae"];
  const COUNT = 90;

  const holder = document.createElement("div");
  holder.setAttribute("aria-hidden", "true");
  holder.style.cssText =
    "position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:9999;";
  document.body.appendChild(holder);

  for (let i = 0; i < COUNT; i++) {
    const p = document.createElement("div");
    const size = 6 + Math.random() * 7;
    const left = Math.random() * 100;
    const delay = Math.random() * 500;
    const duration = 2400 + Math.random() * 1800;
    const drift = (Math.random() - 0.5) * 240;
    const spin = 360 + Math.random() * 720;
    const isStrip = Math.random() < 0.5;

    p.style.cssText =
      "position:absolute;top:-4vh;left:" + left + "vw;" +
      "width:" + size + "px;height:" + (isStrip ? size * 2.4 : size) + "px;" +
      "background:" + COLORS[(Math.random() * COLORS.length) | 0] + ";" +
      "border-radius:" + (isStrip ? "2px" : "50%") + ";" +
      "opacity:" + (0.75 + Math.random() * 0.25) + ";";

    const anim = p.animate(
      [
        { transform: "translate(0,0) rotate(0deg)" },
        {
          transform:
            "translate(" + drift + "px,110vh) rotate(" + spin + "deg)",
        },
      ],
      {
        duration: duration,
        delay: delay,
        easing: "cubic-bezier(0.25, 1, 0.5, 1)",
        fill: "forwards",
      }
    );
    anim.onfinish = function () { p.remove(); };
    holder.appendChild(p);
  }

  setTimeout(function () { holder.remove(); }, 5500);
})();
