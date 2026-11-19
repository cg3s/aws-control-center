export let csrf = '';
export function setCsrf(value: string) { csrf = value; }
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {...init, credentials: 'same-origin', headers: {
    'Content-Type': 'application/json', 'X-AWSCC-CSRF': csrf, ...init.headers,
  }});
  const result = await response.json().catch(() => ({detail: '服务器返回格式无效'}));
  if (response.status === 401 && path !== '/login') window.dispatchEvent(new Event('session-expired'));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `HTTP ${response.status}`);
  return result as T;
}
export const post = <T,>(path: string, body: unknown = {}) => api<T>(path, {method: 'POST', body: JSON.stringify(body)});
export const qs = (params: Record<string, string>) => new URLSearchParams(params).toString();
export type Target = {profile: string; region: string; service: string; instance: string; account_id?: string; partition?: string; generation?: string};
export type Resource = Target & {id: string; name: string; state: string; ip: string; type: string; resource_key: string};
export type Filters = {profile: string; region: string; service: string};
export const gib = (n: number) => (n / 1024 ** 3).toFixed(3) + ' GiB';
export type JobItem = {kind: string; target: Target; options?: object; script_id?: string; connection_id?: string; parameters?: Record<string,string>};
export type TaskItem = {id: string; kind: string; target: Target; status: string; result: Record<string, unknown>; error: string; updated_at: string};
export type Task = {id: string; status: string; actor: string; created_at: string; items: TaskItem[]};
export type Configuration = {id: string; kind: string; name: string; version: number; payload: Record<string, unknown>};

const regionNames:Record<string,string>={'ap-northeast-1':'日本（东京）','ap-northeast-2':'韩国（首尔）','ap-northeast-3':'日本（大阪）','ap-southeast-1':'新加坡','ap-southeast-2':'澳大利亚（悉尼）','ap-southeast-3':'印度尼西亚（雅加达）','ap-southeast-4':'澳大利亚（墨尔本）','ap-south-1':'印度（孟买）','ap-south-2':'印度（海得拉巴）','ap-east-1':'中国香港','us-east-1':'美国（弗吉尼亚）','us-east-2':'美国（俄亥俄）','us-west-1':'美国（北加州）','us-west-2':'美国（俄勒冈）','ca-central-1':'加拿大（中部）','eu-west-1':'爱尔兰','eu-west-2':'英国（伦敦）','eu-west-3':'法国（巴黎）','eu-central-1':'德国（法兰克福）','eu-central-2':'瑞士（苏黎世）','eu-north-1':'瑞典（斯德哥尔摩）','eu-south-1':'意大利（米兰）','eu-south-2':'西班牙','sa-east-1':'巴西（圣保罗）','me-south-1':'巴林','me-central-1':'阿联酋','af-south-1':'南非（开普敦）','cn-north-1':'中国（北京）','cn-northwest-1':'中国（宁夏）'};
export const regionLabel = (value:string) => regionNames[value] ? `${value} · ${regionNames[value]}` : value;
