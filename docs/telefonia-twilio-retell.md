# Telefonía: número de Twilio → agente de Retell por SIP Trunk

La demo prueba los agentes con llamadas web. No integra Twilio porque requiere una cuenta paga. Este documento explica cómo conectaríamos un número real y cómo lo sumaríamos al alta automática para que funcione igual en 50 clínicas o en 100.

Los valores y endpoints salen de la documentación de Retell (`deploy/custom-telephony`, `deploy/twilio`, `api-references/import-phone-number`) y de Twilio (Elastic SIP Trunking API). Los puntos sin verificar están marcados **[confirmar]**.

## Cómo viaja una llamada

```
Paciente ──llama──► Número de Twilio ──► Elastic SIP Trunk ──(origination)──► sip:sip.retellai.com ──► Agente de la clínica
                                              ▲
Agente (llamada saliente) ──(termination: <trunk>.pstn.twilio.com + usuario/contraseña)──┘ ──► red telefónica ──► Paciente
```

- **Llamadas entrantes (origination):** Twilio deriva las llamadas del trunk al servidor SIP de Retell, `sip:sip.retellai.com`. Retell sabe qué agente atiende porque el número está importado en Retell con su agente asignado.
- **Llamadas salientes (termination):** Retell no tiene una dirección SIP propia para salir. Usa la del trunk de Twilio (`<dominio>.pstn.twilio.com`) y se autentica con usuario y contraseña, o con su lista de IPs.

## Pasos para una clínica

| # | Dónde | Qué | API |
|---|---|---|---|
| T1 | Twilio | Crear una **subcuenta** para la clínica: costos, límites y bajas separados por clínica | `POST /2010-04-01/Accounts.json` (`FriendlyName`) **[confirmar]** |
| T2 | Twilio | Buscar y **comprar el número** del país de la clínica | `GET /2010-04-01/Accounts/{Sid}/AvailablePhoneNumbers/ES/Local.json` **[confirmar]**, después `POST .../IncomingPhoneNumbers.json` con `PhoneNumber` (E.164), y `BundleSid`/`AddressSid` donde la regulación lo exige |
| T3 | Twilio | Crear el **Elastic SIP Trunk** | `POST https://trunking.twilio.com/v1/Trunks` con `FriendlyName` y `DomainName` (debe terminar en `pstn.twilio.com`, p. ej. `omni-<slug>.pstn.twilio.com`) |
| T4 | Twilio | **Origination**: hacia dónde van las llamadas entrantes | `POST /v1/Trunks/{TK}/OriginationUrls` con `SipUrl=sip:sip.retellai.com;transport=tcp`, `Priority=10`, `Weight=10`, `Enabled=true`, `FriendlyName` |
| T5 | Twilio | **Autenticación** de las salientes: lista de credenciales con usuario y contraseña generados para la clínica, asociada al trunk | Crear la lista y la credencial: `POST .../SIP/CredentialLists.json` y `.../CredentialLists/{CL}/Credentials.json` **[confirmar]**. Asociarla: `POST /v1/Trunks/{TK}/CredentialLists` con `CredentialListSid` |
| T6 | Twilio | **Asociar el número al trunk** | `POST /v1/Trunks/{TK}/PhoneNumbers` con `PhoneNumberSid`, o pasar `TrunkSid` al comprarlo en T2 |
| T7 | Retell | **Importar el número** y asignarle el agente de la clínica | `POST /import-phone-number` (detalle abajo) |
| T8 | Ambos | **Verificación final** | Detalle abajo |

`POST https://api.retellai.com/import-phone-number`:

```json
{
  "phone_number": "+34910000003",
  "termination_uri": "omni-dental-retiro.pstn.twilio.com",
  "sip_trunk_auth_username": "<usuario de T5>",
  "sip_trunk_auth_password": "<contraseña de T5>",
  "transport": "TCP",
  "inbound_agents": [{"agent_id": "<agent_id de la clínica>", "weight": 1}],
  "outbound_agents": [{"agent_id": "<agent_id de la clínica>", "weight": 1}],
  "nickname": "Clinica Dental Retiro",
  "allowed_inbound_country_list": ["ES"],
  "allowed_outbound_country_list": ["ES"]
}
```

`inbound_agents` y `outbound_agents` son listas con pesos que tienen que sumar 1. Sirven, por ejemplo, para probar un prompt nuevo con el 10% de las llamadas.

**Verificación final (T8):**
- **Twilio:** la prueba de conexión SIP del trunk envía un `OPTIONS`. Si responde 401 o 407, faltan las credenciales; si responde 403, falla la lista de IPs.
- **Llamada saliente de prueba:** Retell llama a un número del equipo (`create-phone-call`). Se comprueba que el agente atiende y que la llamada aparece en `list-calls` con el `agent_id` de la clínica.
- **Llamada entrante de prueba:** si esa parte no se automatiza, se hace una llamada manual de 1 minuto, que queda en el runbook.

## Decisiones

- **Transporte TCP.** Retell recomienda TCP. Twilio usa UDP salvo que se agregue `;transport=tcp` a la dirección de origination, así que se configura igual en los dos lados (T4 y T7). **[confirmar con la primera llamada real]**
- **Autenticación con usuario y contraseña,** únicos por clínica. Twilio exige credenciales o lista de IPs, y si se configuran las dos, se exigen las dos. Se pueden sumar las IPs de señalización de Retell (`18.98.16.120/30`) para mayor seguridad, pero sin credenciales no.
- **Una subcuenta de Twilio por clínica.** Da facturación por clínica, permite dar de baja una sin tocar el resto y limita el alcance si algo se filtra. Para Twilio sigue siendo una sola cuenta principal.
- **Números de España y Argentina.** La compra de Retell (`create-phone-number`) solo ofrece números de Estados Unidos y Canadá, así que los de España y Argentina se compran en Twilio y se importan. Muchos países exigen un *Regulatory Bundle* (identidad y dirección del titular), que se pasa como `BundleSid`/`AddressSid` al comprar. **[confirmar los requisitos de +34 y +54]** El bundle requiere documentación de la clínica: es el **único paso manual**. Se pide en el alta comercial, se carga una vez y el resto es automático.
- **El número que la clínica ya tiene.** Hay dos caminos:
  - **Desvío:** la clínica desvía su línea al número de Twilio cuando no atiende o fuera de horario. Es rápido y no implica riesgo.
  - **Portabilidad:** se lleva su número a Twilio. Tarda semanas y es otro proceso.

  Para empezar, recomiendo el desvío.

## Cómo se automatiza para 50 clínicas

Es una etapa más del alta que ya existe: **subcuenta de GHL → GHL → Retell → Telefonía**.

- **Una función nueva, `alta_telefonia(slug)`**, con los pasos T1 a T8. Emite eventos como el resto (`evento("telefonia", "T3", ...)`), así que el panel muestra el avance, la cola la corre de a una clínica y los errores se reintentan.
- **Se puede repetir sin duplicar.** Cada SID creado (subcuenta, número, trunk, lista de credenciales) se guarda en la base, en una columna nueva `telefonia` de la fila de la clínica. Antes de crear algo, se busca: si ya existe, se reutiliza o se actualiza. Así un reintento continúa donde quedó, sin comprar dos números.
- **Secretos:** el usuario y la contraseña SIP se generan por clínica (`secrets.token_urlsafe`), se guardan en la base y solo viajan a Twilio y a Retell. Las claves de Twilio van en `.env`, como las de GHL y Retell.
- **Antes de comprar:** el panel valida que la clínica tenga país, que exista un bundle si el país lo exige y que el agente de Retell ya esté publicado. Comprar un número es lo único que **cuesta dinero en cada intento**, así que va detrás de esas validaciones. Ante un error después de la compra, se reintenta sin volver a comprar.
- **Lote de 50:** se cargan las 50 webs en el panel, se revisan y quedan en cola. La cola de alta las procesa de a una (GHL + Retell + telefonía, unos 2 o 3 minutos por clínica). Las 50 terminan en unas 2 o 3 horas sin intervención. Lo que falle queda marcado con su paso y su error.
- **Baja de una clínica** (al revés): sacar el número de Retell, liberar el número de Twilio y cerrar la subcuenta. Sirve para no pagar números de clínicas que se fueron.

## Lo que mejora en el agente

- Con llamadas telefónicas reales, Retell entrega el número de quien llama (`from_number`). El middleware ya lo usa: si nadie se registró durante la llamada, crea el contacto en GHL con ese número. El prompt puede dejar de pedir el teléfono y solo confirmarlo.
- El webhook de fin de llamada y la nota en GHL funcionan igual que con las llamadas web.

## Runbook de incidencias de telefonía

| Síntoma | Causa probable | Qué revisar |
|---|---|---|
| La llamada entrante no llega al agente | Dirección de origination o transporte mal configurados | Que la origination del trunk sea `sip:sip.retellai.com;transport=tcp`, y los registros de llamadas de Twilio |
| El agente no puede llamar | Credenciales o dirección del trunk mal configuradas | `OPTIONS`: 401 o 407 indica credenciales, 403 indica la lista de IPs; que `termination_uri` en Retell coincida con el `DomainName` |
| Llega la llamada pero no hay audio | Bloqueo del audio de la llamada (RTP) | Que la red no bloquee los puertos UDP 10000-65535 |
| Atiende el agente de otra clínica | Número importado con otro `agent_id` | `inbound_agents` del número en Retell, comparado con la base |
| No se puede comprar el número | Falta el bundle regulatorio | El bundle de la clínica en Twilio |

## Respuesta corta para el formulario

> "Por cada clínica:
> - Creo una subcuenta de Twilio y compro el número local, con el bundle regulatorio si el país lo pide.
> - Creo un Elastic SIP Trunk:
>   - origination hacia `sip:sip.retellai.com;transport=tcp`, para las entrantes;
>   - una credential list con usuario y contraseña únicos, para las salientes;
>   - el número asociado al trunk.
> - En Retell importo el número con `import-phone-number` (termination URI del trunk, credenciales, TCP y el agente de la clínica en `inbound_agents` y `outbound_agents`).
> - Verifico con un OPTIONS y una llamada de prueba.
>
> Para 50 clínicas es una etapa más del alta automática que ya construí (GHL + Retell):
> - Una cola que procesa de a una clínica, con avance visible.
> - Pasos que se pueden repetir sin duplicar: cada SID se guarda en la base y un reintento retoma donde quedó, sin volver a comprar el número.
> - Validaciones antes de la compra, que es lo único que cuesta en cada intento.
> - Un runbook de incidencias.
>
> El único paso manual es la documentación regulatoria de cada clínica, que se pide una vez en el alta comercial."
