-- PC-1-2 Solution Hub — Supabase ka aik dafa ka setup
-- Supabase → SQL Editor mein paste kar ke Run karein. Sab kuch "if not exists" hai,
-- yani mojooda data ko kuch nahi hota (koi drop/delete nahi hai).
--
-- ⚠️ SECURITY: neeche ki policies anon key ko insert/select ki ijazazat deti hain.
-- App public Streamlit par chalti hai, is liye jo bhi app khole wo rows likh/parh
-- sakega (update/delete ki ijazat jaan kar nahi di gayi). Sirf apni team ke liye
-- rakhna ho to policies ki jagah Supabase Auth lagayein.

-- 1) jo column app bhejti hai magar table mein nahi hain (aap ke case mein components)
alter table public.secure_pc1 add column if not exists components jsonb;
alter table public.secure_pc1 add column if not exists district_allocations jsonb;
alter table public.secure_pc1 add column if not exists verification_status text;
alter table public.secure_pc1 add column if not exists created_at timestamptz
    default now();

-- 2) comments table (agar pehle se hai to ye statement kuch nahi karega)
create table if not exists public.pc1_comments (
    id             bigserial primary key,
    pc1_id         bigint references public.secure_pc1(id) on delete cascade,
    commenter_name text,
    comment_text   text,
    created_at     timestamptz default now()
);

-- 3) RLS: on rakhein, magar anon ke liye insert + select ki policy zaroori hai
alter table public.secure_pc1   enable row level security;
alter table public.pc1_comments enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'secure_pc1'
                 and policyname = 'anon read secure_pc1') then
    create policy "anon read secure_pc1" on public.secure_pc1
      for select to anon, authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'secure_pc1'
                 and policyname = 'anon insert secure_pc1') then
    create policy "anon insert secure_pc1" on public.secure_pc1
      for insert to anon, authenticated with check (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'pc1_comments'
                 and policyname = 'anon read pc1_comments') then
    create policy "anon read pc1_comments" on public.pc1_comments
      for select to anon, authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'pc1_comments'
                 and policyname = 'anon insert pc1_comments') then
    create policy "anon insert pc1_comments" on public.pc1_comments
      for insert to anon, authenticated with check (true);
  end if;
end $$;

-- 4) OPTIONAL — purana insert_secure_pc1 RPC (pgp_sym_encrypt) theek karne ke liye.
-- App ko is RPC ki zarurat nahi rahi (seedha table insert chalta hai), magar aap
-- rakhna chahein to pehle pgcrypto install karein, warna wohi 42883 error aata hai:
-- create extension if not exists pgcrypto with schema extensions;
-- ...phir function ke andar extensions.pgp_sym_encrypt(...) likhein.

-- 5) check: kaunse column ab mojood hain
select column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name in ('secure_pc1', 'pc1_comments')
order by table_name, ordinal_position;
