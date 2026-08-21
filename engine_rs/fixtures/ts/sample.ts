export interface Repo { name: string; }
export class Svc implements Repo {
  method go(): void { this.step(); }
  step(): void {}
}
