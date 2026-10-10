import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Pencil, Power, Trash2 } from 'lucide-react';
import { SidebarRowMenu } from '../SidebarRowMenu';

function setup() {
  const select = vi.fn();
  const rename = vi.fn();
  const remove = vi.fn();
  render(<SidebarRowMenu label="Actions for test project" actions={[
    {label:'Start sandbox', icon:Power, disabled:true, onSelect:vi.fn()},
    {label:'Rename', icon:Pencil, onSelect:rename},
    {label:'Delete', icon:Trash2, destructive:true, onSelect:remove},
  ]}><button onClick={select}>Test project</button></SidebarRowMenu>);
  return {select, rename, remove};
}

describe('sidebar row actions', () => {
  it('opens by keyboard without selecting the row and retains disabled actions', async () => {
    const {select, rename, remove} = setup();
    const user = userEvent.setup();
    screen.getByRole('button', {name:'Actions for test project'}).focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('menuitem', {name:'Start sandbox'})).toHaveAttribute('aria-disabled','true');
    await user.click(screen.getByRole('menuitem', {name:'Rename'}));
    expect(rename).toHaveBeenCalledTimes(1);
    expect(select).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
  });
  it('uses the same callbacks for context menu and keeps deletion last', async () => {
    const {remove, select} = setup();
    fireEvent.contextMenu(screen.getByRole('button',{name:'Test project'}));
    expect(screen.getAllByRole('menuitem').map(el=>el.textContent)).toEqual(['Start sandbox','Rename','Delete']);
    await userEvent.click(screen.getByRole('menuitem',{name:'Delete'}));
    expect(remove).toHaveBeenCalledTimes(1);
    expect(select).not.toHaveBeenCalled();
  });
});
