"use strict";
// Panel de alta de clínicas: lista + revisión + avance. Sin dependencias; consulta la API cada 2 segundos.

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(metodo, ruta, cuerpo) {
  const r = await fetch(ruta, {
    method: metodo, headers: cuerpo ? { "content-type": "application/json" } : {},
    body: cuerpo ? JSON.stringify(cuerpo) : undefined,
  });
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(datos.detail || `HTTP ${r.status}`);
  return datos;
}

function toast(msg) {
  const d = document.createElement("div");
  d.className = "aviso-toast"; d.textContent = msg;
  document.body.appendChild(d); setTimeout(() => d.remove(), 4000);
}

const ESTADOS = {
  en_cola_extraccion: ["En cola: extracción", "cola"], extrayendo: ["Extrayendo", "corre"],
  por_revisar: ["Por revisar", "revisar"], en_cola_alta: ["En cola: alta", "cola"], en_alta: ["Dando de alta", "corre"],
  lista: ["Lista", "ok"], error_extraccion: ["Error en extracción", "error"], error_alta: ["Error en el alta", "error"],
};
const ETAPAS = { extraccion: "Extracción", revision: "Revisión", subcuenta: "Subcuenta (Brave)", ghl: "GHL", retell: "Retell", fin: "Fin", panel: "Panel" };
const estadoHtml = (e) => { const [t, c] = ESTADOS[e] || [e, "cola"]; return `<span class="estado ${c}">${esc(t)}</span>`; };
const progresoHtml = (f) => {
  const cls = f.estado === "lista" ? "ok" : f.estado.startsWith("error") ? "error" : "";
  return `<div class="progreso ${cls}"><div style="width:${Math.round(f.avance || 0)}%"></div></div>`;
};

// ---------------- estado del sistema ----------------
async function refrescarSistema() {
  try {
    const s = await api("GET", "/api/estado");
    const chip = (ok, t) => `<span class="chip ${ok ? "ok" : "mal"}">${ok ? "●" : "○"} ${esc(t)}</span>`;
    $("#sistema").innerHTML = [
      chip(s.brave, "Brave"), chip(s.middleware, "Middleware"), chip(s.ngrok, "ngrok"),
      chip(!s.claves_faltantes.length, s.claves_faltantes.length ? `Faltan: ${s.claves_faltantes.join(", ")}` : "Claves"),
      `<span class="chip info">Extracción: ${s.colas.extraccion.corriendo ? "1 corriendo" : "libre"} · ${s.colas.extraccion.en_cola} en cola</span>`,
      `<span class="chip info">Alta: ${s.colas.alta.corriendo ? "1 corriendo" : "libre"} · ${s.colas.alta.en_cola} en cola</span>`,
    ].join("");
  } catch (e) { $("#sistema").innerHTML = `<span class="chip mal">Panel sin conexión</span>`; }
}

// ---------------- lista ----------------
async function vistaLista() {
  $("#vista").innerHTML = `
    <div class="tarjeta">
      <h2>Extraer clínicas desde su web</h2>
      <textarea id="urls" rows="4" placeholder="Una web por línea, por ejemplo:&#10;https://www.clinica-uno.es&#10;clinica-dos.com"></textarea>
      <div class="fila-flex" style="margin-top:10px">
        <button class="btn primario" id="extraer">Extraer</button>
        <span class="suave chico-txt">Se procesan de a una. Cada web tarda 1 a 3 minutos y cuesta ~$0,30 de Claude.</span>
      </div>
    </div>
    <div class="tarjeta"><h2>Clínicas</h2><div id="tabla"></div></div>`;
  $("#extraer").onclick = async () => {
    const urls = $("#urls").value.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return toast("Pegá al menos una web");
    try {
      const r = await api("POST", "/api/extracciones", { urls });
      $("#urls").value = r.rechazadas.join("\n");
      toast(`${r.ids.length} encolada(s)` + (r.rechazadas.length ? `. No parecen webs: ${r.rechazadas.join(", ")}` : ""));
      refrescarLista();
    } catch (e) { toast(e.message); }
  };
  await refrescarLista();
}

async function refrescarLista() {
  const tabla = $("#tabla");
  if (!tabla) return;
  const filas = await api("GET", "/api/clinicas");
  if (!filas.length) { tabla.innerHTML = `<div class="vacio">Todavía no hay clínicas. Pegá webs arriba para empezar.</div>`; return; }
  tabla.innerHTML = `<table class="tabla"><thead><tr><th>Clínica</th><th>Estado</th><th style="width:38%">Avance</th><th></th></tr></thead><tbody>
    ${filas.map((f) => `<tr class="${f.origen_id ? "sede" : ""}">
      <td><a href="#/c/${f.id}"><strong>${esc(f.nombre || "(extrayendo…)")}</strong></a><br>
          <span class="suave chico-txt">${esc(f.url)}${f.slug ? ` · ${esc(f.slug)}` : ""}</span></td>
      <td>${estadoHtml(f.estado)}</td>
      <td><span class="chico-txt">${f.etapa && ETAPAS[f.etapa] ? `<b>${esc(ETAPAS[f.etapa])}</b> · ` : ""}${esc(f.mensaje || "")}</span>${progresoHtml(f)}</td>
      <td style="white-space:nowrap">
        <a class="btn chico" href="#/c/${f.id}">${f.estado === "por_revisar" ? "Revisar" : "Ver"}</a>
        ${f.estado.startsWith("error") ? `<button class="btn chico" onclick="reintentar(${f.id})">Reintentar</button>` : ""}
        ${["por_revisar", "error_extraccion", "error_alta", "en_cola_extraccion", "en_cola_alta"].includes(f.estado)
          ? `<button class="btn chico peligro" onclick="descartar(${f.id})">Descartar</button>` : ""}
      </td></tr>`).join("")}</tbody></table>`;
}

async function reintentar(id) {
  try {
    const r = await api("POST", `/api/clinicas/${id}/reintentar`);
    if (r.ok === false) toast("No se pudo reintentar: " + r.problemas.join("; "));
    else toast("Reintento encolado");
  } catch (e) { toast(e.message); }
  refrescar();
}
async function descartar(id) {
  if (!confirm("¿Descartar esta clínica? Se saca de la lista (no borra nada en GHL ni Retell).")) return;
  try { await api("DELETE", `/api/clinicas/${id}`); location.hash = "#/"; refrescar(); } catch (e) { toast(e.message); }
}

// ---------------- detalle / revisión ----------------
const PAISES = ["ES", "AR", "MX", "CL", "UY", "CO", "PE", "US"];
const ZONAS = ["Europe/Madrid", "Atlantic/Canary", "America/Argentina/Buenos_Aires", "America/Argentina/Mendoza",
  "America/Argentina/Cordoba", "America/Mexico_City", "America/Santiago", "America/Montevideo", "America/Bogota", "America/Lima"];
const DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"];
const EDITABLE = ["por_revisar", "error_alta"];

let actual = null;       // {id, datos, estado, ...}
let ultimoEvento = 0;
let etapaPrevia = null;

const leer = (obj, ruta) => ruta.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
function escribir(obj, ruta, valor) {
  const ks = ruta.split("."); let o = obj;
  ks.slice(0, -1).forEach((k) => { if (o[k] == null || typeof o[k] !== "object") o[k] = {}; o = o[k]; });
  o[ks.at(-1)] = valor;
}

async function vistaClinica(id) {
  ultimoEvento = 0; etapaPrevia = null;
  const d = await api("GET", `/api/clinicas/${id}`);
  actual = d;
  $("#vista").innerHTML = `
    <div class="tarjeta" id="cabecera"></div>
    <div id="cuerpo"></div>
    <div class="tarjeta"><h2>Registro de actividad</h2><div class="eventos" id="eventos"><div class="suave">Sin eventos todavía.</div></div></div>`;
  pintarCabecera(d);
  pintarCuerpo(d);
  await refrescarEventos();
}

function pintarCabecera(d) {
  const links = d.links || {};
  $("#cabecera").innerHTML = `
    <div class="fila-flex" style="justify-content:space-between">
      <div><a href="#/" class="chico-txt">← Clínicas</a><h1>${esc(d.nombre || d.url)}</h1>
        <span class="suave">${esc(d.url)}</span></div>
      <div>${estadoHtml(d.estado)}</div>
    </div>
    <div style="margin-top:10px"><span class="chico-txt">${d.etapa && ETAPAS[d.etapa] ? `<b>${esc(ETAPAS[d.etapa])}</b> · ` : ""}${esc(d.mensaje || "")}</span>${progresoHtml(d)}</div>
    ${Object.keys(links).length ? `<div class="links" style="margin-top:12px">
      ${links.ghl ? `<a href="${esc(links.ghl)}" target="_blank">Subcuenta en GHL ↗</a>` : ""}
      ${links.retell ? `<a href="${esc(links.retell)}" target="_blank">Agente en Retell ↗</a>` : ""}
      ${links.llamadas ? `<a href="${esc(links.llamadas)}" target="_blank">Página de llamadas ↗</a>` : ""}</div>` : ""}
    ${d.error ? `<div class="caja-error"><b>Error:</b> ${esc(d.error)}
      ${d.captura ? `<br><a href="/api/clinicas/${d.id}/captura?t=${Date.now()}" target="_blank">Ver captura del navegador</a>` : ""}
      <div style="margin-top:8px"><button class="btn chico" onclick="reintentar(${d.id})">Reintentar</button></div></div>` : ""}`;
}

function pintarCuerpo(d) {
  const cuerpo = $("#cuerpo");
  if (!d.datos) { cuerpo.innerHTML = ""; return; }
  const editable = EDITABLE.includes(d.estado);
  cuerpo.innerHTML = `<div class="grilla-revision">
      <div class="tarjeta" id="formulario"></div>
      <div class="tarjeta lateral" id="revision"></div>
    </div>`;
  pintarFormulario(editable);
  pintarRevision();
}

// --- formulario ---
function fuentesDe(campo) {
  return (actual.fuentes || []).filter((f) => f.campo.split(".").at(-1).replace(/\[\d+\]$/, "") === campo).slice(0, 2);
}
function problemaDe(ruta) {
  return (actual.problemas || []).some((p) => p.startsWith(ruta + ":") || p.startsWith(ruta + "["));
}

function campo(ruta, etiqueta, { tipo = "text", ancho = false, opciones = null, fuente = null, lista = null, numero = false } = {}) {
  const v = leer(actual.datos, ruta);
  const mal = v === "COMPLETAR" || problemaDe(ruta);
  const fuentes = fuente ? fuentesDe(fuente).map((f) => `<a class="fuente" href="${esc(f.url)}" target="_blank" title="${esc(f.campo)}">fuente</a>`).join(" ") : "";
  let control;
  if (opciones) control = `<select data-ruta="${ruta}" class="${mal ? "falta" : ""}">${opciones.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}${!opciones.includes(v) ? `<option selected>${esc(v)}</option>` : ""}</select>`;
  else if (tipo === "textarea") control = `<textarea data-ruta="${ruta}" rows="2" class="${mal ? "falta" : ""}">${esc(v)}</textarea>`;
  else control = `<input data-ruta="${ruta}" ${numero ? 'data-numero="1" type="number"' : `type="${tipo}"`} value="${esc(v)}" class="${mal ? "falta" : ""}" ${lista ? `list="${lista}"` : ""}>`;
  return `<div class="campo ${ancho ? "ancho" : ""}"><label><span>${esc(etiqueta)}</span>${fuentes}</label>${control}</div>`;
}

function editorLista(ruta, columnas, nuevo, titulo) {
  const items = leer(actual.datos, ruta) || [];
  const plantilla = columnas ? columnas.map((c) => c.ancho || "1fr").join(" ") + " auto" : "1fr auto";
  return `<h3>${esc(titulo)} (${items.length})</h3><div class="lista-editor">
    ${items.map((it, i) => `<div class="item" style="grid-template-columns:${plantilla}">
      ${columnas ? columnas.map((c) => c.area
        ? `<textarea data-ruta="${ruta}.${i}.${c.key}" rows="2" placeholder="${esc(c.label)}">${esc(it[c.key])}</textarea>`
        : `<input data-ruta="${ruta}.${i}.${c.key}" value="${esc(it[c.key])}" placeholder="${esc(c.label)}" class="${it[c.key] === "COMPLETAR" ? "falta" : ""}">`).join("")
        : `<input data-ruta="${ruta}.${i}" value="${esc(it)}">`}
      <button class="btn chico peligro" data-quitar="${ruta}" data-i="${i}" title="Quitar">✕</button></div>`).join("")}
    <button class="btn chico" data-agregar="${ruta}" data-nuevo='${esc(JSON.stringify(nuevo))}'>+ Agregar</button></div>`;
}

function editorHorario() {
  const h = leer(actual.datos, "calendario.horario");
  const horario = h && typeof h === "object" ? h : {};
  return `<h3>Horario de atención</h3>
    ${h === "COMPLETAR" || problemaDe("calendario.horario") ? `<div class="problemas chico-txt">Revisá el horario: días sin tramos = cerrado.</div>` : ""}
    ${DIAS.map((dia) => `<div class="dia"><b>${dia}</b><div>
      ${(horario[dia] || []).map((t, i) => `<span class="tramo">
        <input data-tramo="${dia}" data-i="${i}" data-j="0" value="${esc(t[0])}" maxlength="5" placeholder="09:00" class="${/^([01]\d|2[0-3]):[0-5]\d$/.test(t[0]) ? "" : "falta"}"> –
        <input data-tramo="${dia}" data-i="${i}" data-j="1" value="${esc(t[1])}" maxlength="5" placeholder="14:00" class="${/^([01]\d|2[0-3]):[0-5]\d$/.test(t[1]) ? "" : "falta"}">
        <button class="btn chico peligro" data-quitar-tramo="${dia}" data-i="${i}">✕</button></span>`).join("")}
      ${(horario[dia] || []).length ? "" : `<span class="suave chico-txt">cerrado</span> `}
      <button class="btn chico" data-agregar-tramo="${dia}">+ tramo</button></div></div>`).join("")}`;
}

function pintarFormulario(editable) {
  const d = actual.datos;
  const cv = d.custom_values || {};
  $("#formulario").innerHTML = `
    <datalist id="zonas">${ZONAS.map((z) => `<option value="${z}">`).join("")}</datalist>
    <h2>Datos de la clínica</h2>
    <div class="campos">
      ${campo("clinica", "Nombre (subcuenta y agente)", { ancho: true })}
      ${campo("slug", "Slug")}
      ${campo("perfil.web", "Web")}
    </div>
    <h3>Perfil</h3><div class="campos">
      ${campo("perfil.telefono", "Teléfono", { fuente: "telefono" })}
      ${campo("perfil.email", "Email de la clínica", { fuente: "email" })}
      ${campo("perfil.direccion", "Dirección", { fuente: "direccion", ancho: true })}
      ${campo("perfil.ciudad", "Ciudad", { fuente: "ciudad" })}
      ${campo("perfil.provincia", "Provincia", { fuente: "provincia" })}
      ${campo("perfil.cp", "Código postal", { fuente: "cp" })}
      ${campo("perfil.pais", "País", { opciones: PAISES })}
      ${campo("perfil.timezone", "Zona horaria", { lista: "zonas" })}
    </div>
    <h3>Contacto de la subcuenta (dueño en GHL)</h3><div class="campos">
      ${campo("perfil.contacto.nombre", "Nombre")}${campo("perfil.contacto.apellido", "Apellido")}
      ${campo("perfil.contacto.email", "Email")}
    </div>
    <h3>Profesional (usuario y calendario de GHL)</h3><div class="campos">
      ${campo("profesional.nombre", "Nombre")}${campo("profesional.apellido", "Apellido")}
      ${campo("profesional.email", "Email (recibe los turnos)")}${campo("profesional.telefono", "Teléfono")}
    </div>
    <h3>Calendario</h3><div class="campos">
      ${campo("calendario.nombre", "Nombre del calendario")}
      ${campo("calendario.duracion_min", "Duración del turno (min)", { numero: true })}
    </div>
    ${editorHorario()}
    <h3>Agente de Retell</h3><div class="campos">
      ${campo("agente.nombre_asistente", "Nombre del asistente")}
      ${campo("agente.idioma", "Idioma", { opciones: ["es-ES", "es-419"] })}
      ${campo("agente.voz", "Voz (voice_id)")}
      ${campo("agente.saludo", "Saludo", { tipo: "textarea", ancho: true })}
    </div>
    <h3>Urgencias</h3><div class="campos">
      ${campo("urgencias.accion", "Acción", { opciones: ["tomar_datos", "derivar"] })}
      ${campo("urgencias.telefono", "Teléfono (si deriva)")}
      ${campo("urgencias.texto", "Qué dice el agente", { tipo: "textarea", ancho: true })}
    </div>
    <h3>Contacto extra</h3><div class="campos">
      ${campo("contacto_extra.whatsapp", "WhatsApp")}
    </div>
    ${editorLista("contacto_extra.telefonos", null, "", "Otros teléfonos")}
    <h3>Custom values de GHL</h3>
    <div class="campos">${Object.keys(cv).map((k) => campo(`custom_values.${k}`, k, { ancho: k === "Horario" })).join("")}</div>
    ${editorLista("servicios", [{ key: "nombre", label: "Servicio", ancho: "1.2fr" }, { key: "precio", label: "Precio" }], { nombre: "", precio: "Consultar en la clinica" }, "Servicios y precios")}
    ${editorLista("faq", [{ key: "pregunta", label: "Pregunta", area: 1 }, { key: "respuesta", label: "Respuesta", area: 1, ancho: "1.6fr" }], { pregunta: "", respuesta: "" }, "Preguntas frecuentes")}
    ${editorLista("equipo", [{ key: "titulo", label: "Título", ancho: "70px" }, { key: "nombre", label: "Nombre" }, { key: "apellido", label: "Apellido" }, { key: "especialidad", label: "Especialidad", ancho: "1.4fr" }], { titulo: "", nombre: "", apellido: "", especialidad: "" }, "Equipo")}
    ${editorLista("informacion_util", null, "", "Información útil para el paciente")}
    <div class="acciones">
      ${editable ? `<button class="btn" id="guardar">Guardar borrador</button>
        <button class="btn" id="ver-prompt">Ver prompt del agente</button>
        <button class="btn primario" id="validar">Validar y dar de alta</button>
        <button class="btn peligro" onclick="descartar(${actual.id})">Descartar</button>`
      : `<button class="btn" id="ver-prompt">Ver prompt del agente</button>
         <span class="suave chico-txt">Solo lectura: ${esc((ESTADOS[actual.estado] || [actual.estado])[0])}</span>`}
    </div>`;
  const form = $("#formulario");
  if (!editable) form.querySelectorAll("input, textarea, select, button[data-quitar], button[data-agregar], button[data-agregar-tramo], button[data-quitar-tramo]").forEach((el) => (el.disabled = true));
  form.oninput = (e) => {
    const el = e.target;
    if (el.dataset.ruta) {
      const ruta = el.dataset.ruta.replace(/\.(\d+)(?=\.|$)/g, ".$1");
      escribir(actual.datos, ruta, el.dataset.numero ? parseInt(el.value, 10) || 0 : el.value);
      el.classList.toggle("falta", el.value === "COMPLETAR");
    } else if (el.dataset.tramo) {
      actual.datos.calendario.horario[el.dataset.tramo][+el.dataset.i][+el.dataset.j] = el.value;
      el.classList.toggle("falta", !/^([01]\d|2[0-3]):[0-5]\d$/.test(el.value));
    }
  };
  form.onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const h = () => { const c = actual.datos.calendario; if (!c.horario || typeof c.horario !== "object") c.horario = {}; return c.horario; };
    if (b.dataset.agregar) { const l = leer(actual.datos, b.dataset.agregar) || []; l.push(JSON.parse(b.dataset.nuevo)); escribir(actual.datos, b.dataset.agregar, l); }
    else if (b.dataset.quitar) { leer(actual.datos, b.dataset.quitar).splice(+b.dataset.i, 1); }
    else if (b.dataset.agregarTramo) { const x = h(); (x[b.dataset.agregarTramo] ||= []).push(["09:00", "14:00"]); }
    else if (b.dataset.quitarTramo) { const x = h(); x[b.dataset.quitarTramo].splice(+b.dataset.i, 1); if (!x[b.dataset.quitarTramo].length) delete x[b.dataset.quitarTramo]; }
    else return;
    pintarFormulario(true);
  };
  if ($("#guardar")) $("#guardar").onclick = guardar;
  if ($("#validar")) $("#validar").onclick = validarYAlta;
  $("#ver-prompt").onclick = verPrompt;
}

function limpiarDatos() {
  const d = structuredClone(actual.datos);
  const h = d.calendario && d.calendario.horario;
  if (h && typeof h === "object") for (const dia of Object.keys(h)) if (!h[dia] || !h[dia].length) delete h[dia];
  if (d.contacto_extra) {
    d.contacto_extra.telefonos = (d.contacto_extra.telefonos || []).filter(Boolean);
    if (!d.contacto_extra.telefonos.length) delete d.contacto_extra.telefonos;
    if (!d.contacto_extra.whatsapp) delete d.contacto_extra.whatsapp;
  }
  d.informacion_util = (d.informacion_util || []).filter((x) => x.trim());
  return d;
}

async function guardar() {
  try {
    const r = await api("PUT", `/api/clinicas/${actual.id}`, limpiarDatos());
    actual.problemas = r.problemas; toast(r.problemas.length ? `Guardado. Quedan ${r.problemas.length} problema(s)` : "Guardado: listo para validar");
    pintarFormulario(true); pintarRevision();
  } catch (e) { toast(e.message); }
}

async function validarYAlta() {
  try {
    const r = await api("POST", `/api/clinicas/${actual.id}/validar`, limpiarDatos());
    actual.problemas = r.problemas;
    if (!r.ok) { toast(`No se puede dar de alta: ${r.problemas.length} problema(s)`); pintarFormulario(true); pintarRevision(); $("#revision").scrollTop = 0; return; }
    toast("Validada: en cola para el alta");
    vistaClinica(actual.id);
  } catch (e) { toast(e.message); }
}

async function verPrompt() {
  try {
    const r = await api("POST", `/api/clinicas/${actual.id}/prompt`, limpiarDatos());
    $("#dialogo-titulo").textContent = "Prompt del agente";
    $("#dialogo-cuerpo").innerHTML = `<p class="chico-txt suave">Saludo: «${esc(actual.datos.agente?.saludo)}»</p><pre class="prompt">${esc(r.prompt)}</pre>`;
    $("#dialogo").showModal();
  } catch (e) { toast(e.message); }
}

function pintarRevision() {
  const r = actual.revision || {};
  const lista = (titulo, items, cls = "") => items && items.length
    ? `<h3>${esc(titulo)} (${items.length})</h3><ul class="${cls}">${items.map((i) => `<li>${esc(typeof i === "string" ? i : JSON.stringify(i))}</li>`).join("")}</ul>` : "";
  const fuentes = (actual.fuentes || []);
  $("#revision").innerHTML = `<h2>Revisión</h2>
    ${actual.problemas && actual.problemas.length ? lista("Para poder dar de alta", actual.problemas, "problemas")
      : EDITABLE.includes(actual.estado) ? `<div class="chip ok">Sin problemas: se puede validar</div>` : ""}
    ${lista("La web no lo publica", r.faltantes)}
    ${lista("Contradicciones entre páginas", r.contradicciones)}
    ${lista("Valores por defecto (revisar)", r.valores_por_defecto)}
    ${lista("Observaciones", r.observaciones)}
    ${fuentes.length ? `<h3>Fuentes (${fuentes.length})</h3><ul class="chico-txt">${fuentes.map((f) => `<li>${esc(f.campo)}: <a href="${esc(f.url)}" target="_blank">${esc(f.url.replace(/^https?:\/\//, ""))}</a></li>`).join("")}</ul>` : ""}`;
}

async function refrescarEventos() {
  if (!actual) return;
  const evs = await api("GET", `/api/clinicas/${actual.id}/eventos?desde=${ultimoEvento}`);
  if (!evs.length) return;
  const caja = $("#eventos");
  if (!ultimoEvento) caja.innerHTML = "";
  const abajo = caja.scrollHeight - caja.scrollTop - caja.clientHeight < 40;
  for (const ev of evs) {
    ultimoEvento = Math.max(ultimoEvento, ev.id);
    const nueva = ev.etapa !== etapaPrevia && ev.nivel !== "detalle";
    if (nueva) etapaPrevia = ev.etapa;
    const hora = new Date(ev.ts * 1000).toLocaleTimeString("es", { hour12: false });
    const div = document.createElement("div");
    div.className = `ev ${ev.nivel} ${nueva && caja.children.length ? "etapa-nueva" : ""}`;
    div.innerHTML = `<span class="hora">${hora}</span><span>${nueva ? `<b>${esc(ETAPAS[ev.etapa] || ev.etapa)}</b>` : ev.nivel === "detalle" ? "" : `<span class="suave">${esc(ev.paso)}</span>`}</span><span>${esc(ev.mensaje)}</span>`;
    caja.appendChild(div);
  }
  if (abajo || evs.length === caja.children.length) caja.scrollTop = caja.scrollHeight;
}

async function refrescarDetalle() {
  if (!actual) return;
  try {
    const d = await api("GET", `/api/clinicas/${actual.id}`);
    const cambioEstado = d.estado !== actual.estado;
    const datosLocales = actual.datos;
    Object.assign(actual, d);
    pintarCabecera(d);
    if (cambioEstado || !$("#formulario") && d.datos) {
      pintarCuerpo(d);
    } else if (EDITABLE.includes(d.estado)) {
      actual.datos = datosLocales;  // no pisar lo que se está editando
    } else if ($("#revision")) pintarRevision();
    await refrescarEventos();
  } catch (e) { if (/No existe/.test(e.message)) location.hash = "#/"; }
}

// ---------------- rutas ----------------
function ruta() {
  const m = location.hash.match(/^#\/c\/(\d+)/);
  if (m) vistaClinica(+m[1]).catch((e) => { toast(e.message); location.hash = "#/"; });
  else { actual = null; vistaLista(); }
}
function refrescar() {
  refrescarSistema();
  if (actual) refrescarDetalle(); else refrescarLista().catch(() => {});
}
window.addEventListener("hashchange", ruta);
ruta(); refrescarSistema();
setInterval(refrescar, 2000);
