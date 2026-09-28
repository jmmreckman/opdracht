function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function toonAfwijzen(id) {
  document.getElementById("afwijzen-" + id).classList.toggle("verborgen");
}

function initIntake() {
  const chat = document.getElementById("chat");
  const form = document.getElementById("chat-form");
  const veld = document.getElementById("chat-tekst");
  const knop = document.getElementById("chat-knop");
  const voorstel = document.getElementById("voorstel");
  const id = chat.dataset.intake;
  chat.scrollTop = chat.scrollHeight;

  veld.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) form.requestSubmit();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const tekst = veld.value.trim();
    if (!tekst) return;
    chat.insertAdjacentHTML("beforeend", `<div class="bubbel jij">${esc(tekst)}</div>`);
    chat.insertAdjacentHTML("beforeend", `<div class="bubbel ai denkt"><span class="spinner"></span> denkt na…</div>`);
    chat.scrollTop = chat.scrollHeight;
    veld.value = "";
    knop.disabled = true;
    try {
      const r = await fetch(`/api/intake/${id}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({tekst}),
      });
      const data = await r.json();
      chat.querySelector(".denkt")?.remove();
      if (!r.ok) throw new Error(data.fout || "Er ging iets mis");
      const laatste = data.weergave[data.weergave.length - 1];
      chat.insertAdjacentHTML("beforeend", `<div class="bubbel ai">${esc(laatste.tekst)}</div>`);
      if (data.voorstel) vulVoorstel(voorstel, data.voorstel);
    } catch (err) {
      chat.querySelector(".denkt")?.remove();
      chat.insertAdjacentHTML("beforeend", `<div class="bubbel fout">${esc(err.message)}</div>`);
      veld.value = tekst;
    }
    knop.disabled = false;
    chat.scrollTop = chat.scrollHeight;
  });
}

function vulVoorstel(form, v) {
  for (const k of ["titel", "type", "brief", "criteria", "deadline", "deelbaar", "autonomie"]) {
    if (form.elements[k] && v[k] !== undefined) form.elements[k].value = v[k];
  }
  form.classList.remove("verborgen");
  form.scrollIntoView({behavior: "smooth", block: "start"});
}

function initOpdracht() {
  const bezig = document.getElementById("bezig");
  if (!bezig) return;
  const id = bezig.dataset.opdracht;
  let wasBezig = bezig.dataset.bezig === "1";
  let rondes = 0;
  // Na "Ronde nu draaien" duurt het even voor de ronde echt begint.
  const netGestart = document.referrer === location.href && !wasBezig;
  async function check() {
    rondes++;
    try {
      const r = await fetch(`/api/opdracht/${id}/status`);
      const s = await r.json();
      if (s.ronde_bezig) {
        bezig.classList.remove("verborgen");
        wasBezig = true;
      } else if (wasBezig) {
        location.reload();
        return;
      }
    } catch (e) { /* netwerk even weg: volgende keer weer */ }
    if (wasBezig || (netGestart && rondes < 10)) setTimeout(check, 4000);
  }
  if (wasBezig || netGestart) setTimeout(check, 2000);
}
