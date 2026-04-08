# Migracao do Frontend para a PDF API v2

## Objetivo

Migrar o frontend das rotas atuais para a `v2` sem quebrar uploads, downloads nem a experiencia atual do utilizador.

## O que nao muda

- Os pedidos continuam a ser `multipart/form-data`.
- O ficheiro continua a ser enviado no campo `file`.
- O campo `options` continua a existir e continua a ser uma string JSON.
- A maioria das tools continua a responder com download binario direto.
- O header `X-API-Key` continua a funcionar.

## O que muda no FE

### 1. Trocar as rotas para `/v2`

Substituir:

- `/echo` por `/v2/echo`
- `/compress` por `/v2/compress`
- `/convert` por `/v2/convert`
- `/flatten` por `/v2/flatten`
- `/fill-form` por `/v2/fill-form`
- `/ocr` por `/v2/ocr`
- `/pdfa` por `/v2/pdfa`
- `/pdf-to-image` por `/v2/pdf-to-image`
- `/protect` por `/v2/protect`
- `/redact` por `/v2/redact`
- `/health` por `/v2/health`

### 2. Atualizar o parser de erros

Na `v1`, os erros eram assim:

```json
{
  "error": "Formato nao suportado"
}
```

Na `v2`, os erros passam a ser assim:

```json
{
  "error": {
    "code": "unsupported_media_type",
    "message": "Formato nao suportado.",
    "details": null,
    "requestId": "2f7f5b0e-4d6a-4f9f-b6a9-1d5d8d93a420"
  }
}
```

No FE, passa a ser preciso ler:

- `error.code`
- `error.message`
- `error.details`
- `error.requestId`

Recomendacao:

- mostrar `error.message` ao utilizador
- usar `error.code` para regras especificas no client
- guardar `error.requestId` nos logs, Sentry ou suporte

### 3. Passar a ler `X-Request-ID`

Todas as respostas incluem `X-Request-ID`.

No FE convem:

- guardar este valor nos erros
- anexar este valor ao tracking interno
- mostrar este valor em modo debug/admin quando uma tool falhar

### 4. Auth

O FE pode continuar com:

```http
X-API-Key: <token>
```

Mas a `v2` tambem aceita:

```http
Authorization: Bearer <token>
```

Recomendacao:

- curto prazo: manter `X-API-Key`
- medio prazo: migrar para `Authorization: Bearer`

### 5. Validacao de `options` ficou estrita

O FE continua a enviar:

- `file`
- `options`

Mas agora a `v2` valida o JSON de forma estrita. Isto significa:

- JSON mal formado deixa de gerar erro generico
- chaves extra passam a falhar
- valores invalidos passam a falhar com erro estruturado

Exemplos:

- `pdf-to-image` aceita apenas `{"format":"png"}` ou `{"format":"jpeg"}`
- `ocr` aceita apenas linguas suportadas
- `pdfa` aceita apenas conformidades suportadas
- `fill-form` exige `fields`
- `redact` valida `strategy`, `customText` e `regexPattern`

### 6. `fill-form` ficou mais seguro

Na `v2`, `/v2/fill-form` falha com `422 unknown_form_fields` se o FE enviar nomes de campos que nao existem no PDF.

Isto obriga o FE a:

- validar melhor o mapping dos campos
- registar `details.unknownFields` quando a resposta falhar

Esta mudanca evita falsos sucessos em que o pedido devolvia `200` mas o PDF nao era realmente preenchido.

## Estrutura recomendada no FE

### Cliente HTTP

Centralizar toda a integracao da API PDF num unico modulo, por exemplo:

- `src/lib/pdfApi.ts`
- `src/services/pdf-api.ts`
- `src/api/pdf.ts`

Esse modulo deve concentrar:

- base URL
- headers de auth
- construcao do `FormData`
- parsing de erro
- leitura do `X-Request-ID`

### Builder de `FormData`

Manter o formato:

```ts
const formData = new FormData();
formData.append("file", file);
formData.append("options", JSON.stringify(options ?? {}));
```

### Parser de erro

Implementar algo deste genero:

```ts
export async function parsePdfApiError(response: Response) {
  const body = await response.json().catch(() => null);

  if (body?.error?.message) {
    return {
      code: body.error.code ?? "unknown_error",
      message: body.error.message,
      details: body.error.details ?? null,
      requestId: body.error.requestId ?? response.headers.get("X-Request-ID"),
    };
  }

  if (typeof body?.error === "string") {
    return {
      code: "legacy_error",
      message: body.error,
      details: null,
      requestId: response.headers.get("X-Request-ID"),
    };
  }

  return {
    code: "unknown_error",
    message: "Erro inesperado da API.",
    details: null,
    requestId: response.headers.get("X-Request-ID"),
  };
}
```

## `options` por tool

### `echo`, `compress`, `flatten`

Enviar:

```json
{}
```

### `convert`

Enviar:

```json
{}
```

Nota:

- a API agora tenta inferir o tipo do ficheiro pela extensao quando o MIME type nao vem bem preenchido

### `ocr`

Enviar por exemplo:

```json
{
  "language": "english"
}
```

Valores aceites:

- `english`
- `spanish`
- `french`
- `german`
- `portuguese`
- `italian`
- `chinese`
- `jpn`

### `pdfa`

Enviar por exemplo:

```json
{
  "conformance": "pdfa-2b"
}
```

Valores aceites:

- `pdfa-1b`
- `pdfa-2b`
- `pdfa-3b`

### `pdf-to-image`

Enviar por exemplo:

```json
{
  "format": "png"
}
```

Valores aceites:

- `png`
- `jpeg`

### `protect`

Enviar:

```json
{
  "userPassword": "secret123"
}
```

### `fill-form`

Enviar:

```json
{
  "fields": {
    "Name": "John Doe",
    "City": "Lisboa",
    "Agree": true
  }
}
```

### `redact`

Email:

```json
{
  "strategy": "email"
}
```

Texto custom:

```json
{
  "strategy": "custom",
  "customText": "CONFIDENTIAL"
}
```

Regex:

```json
{
  "strategy": "regex",
  "regexPattern": "\\d{3}-\\d{2}-\\d{4}"
}
```

## Sequencia de migracao recomendada

1. Centralizar o client da PDF API num unico modulo.
2. Criar suporte a `/v2` atras de feature flag.
3. Atualizar o parser de erros.
4. Migrar primeiro as tools de menor risco:
   - `echo`
   - `compress`
   - `flatten`
   - `pdf-to-image`
5. Migrar depois as tools mais estritas:
   - `protect`
   - `ocr`
   - `pdfa`
   - `redact`
   - `fill-form`
6. Monitorizar erros e `requestId`.
7. Remover fallback da `v1` quando a producao estiver estavel.

## Checklist para o FE

- trocar endpoints para `/v2/...`
- manter `multipart/form-data`
- continuar a enviar `options` como JSON string
- atualizar parser de erros
- guardar `X-Request-ID`
- suportar `error.details`
- validar melhor mappings de `fill-form`
- rever mensagens de erro por codigo
- opcionalmente migrar auth para `Authorization: Bearer`
