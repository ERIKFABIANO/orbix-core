-- No Supabase este trigger vive no schema auth, que o dump de `public` não traz.
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
