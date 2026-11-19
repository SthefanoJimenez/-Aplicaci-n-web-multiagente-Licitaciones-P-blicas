import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';
import exec from 'k6/execution';

// Solo la demo local, sin servicios de modelos ni base compartida.
const base = __ENV.BASE_URL || 'http://127.0.0.1:5050';
if (!/^http:\/\/(127\.0\.0\.1|localhost):5050$/.test(base)) {
  throw new Error('Esta prueba solo admite la demostración local en el puerto 5050.');
}
const trends = {
  uno: new Trend('listado_1_usuario', true),
  tres: new Trend('listado_3_usuarios', true),
  cinco: new Trend('listado_5_usuarios', true),
};
export const options = {
  scenarios: {
    uno: {executor: 'per-vu-iterations', vus: 1, iterations: 10, maxDuration: '4s', gracefulStop: '0s'},
    tres: {executor: 'per-vu-iterations', vus: 3, iterations: 10, maxDuration: '4s', startTime: '5s', gracefulStop: '0s'},
    cinco: {executor: 'per-vu-iterations', vus: 5, iterations: 10, maxDuration: '4s', startTime: '10s', gracefulStop: '0s'},
  },
  thresholds: { http_req_failed: ['rate==0'], checks: ['rate==1'] },
  summaryTrendStats: ['med', 'p(90)', 'max', 'count'],
};
export function setup() {
  const r = http.get(base + '/convocatorias', {timeout: '3s', redirects: 0});
  if (r.status !== 200 || !r.body.includes('DEMOSTRACIÓN VERDE') || !r.body.includes('9100103')) {
    throw new Error('No se encontró la demo ficticia; inicie scripts/demostrar_hu010.py.');
  }
}
export default function () {
  const r = http.get(base + '/convocatorias', {timeout: '3s', redirects: 0});
  trends[exec.scenario.name].add(r.timings.duration);
  check(r, {
    'HTTP 200': v => v.status === 200,
    'conserva los tres casos ficticios': v => ['9100101','9100102','9100103'].every(code => v.body.includes(code)),
  });
  sleep(0.1);
}
