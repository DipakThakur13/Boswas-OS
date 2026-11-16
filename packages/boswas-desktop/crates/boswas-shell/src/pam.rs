//! Password check through PAM, for the lock screen.
//!
//! This is the only module of the shell with foreign function calls: a
//! minimal binding to the four libpam functions an authentication needs.
//! The lock screen runs as the logged-in user, so PAM verifies that user's
//! own password (pam_unix uses its setgid helper for that); no privilege
//! is involved.

use std::{
    ffi::{c_char, c_int, c_void, CString},
    ptr,
};

use zeroize::Zeroizing;

const PAM_SUCCESS: c_int = 0;
const PAM_BUF_ERR: c_int = 5;
const PAM_CONV_ERR: c_int = 19;
const PAM_PROMPT_ECHO_OFF: c_int = 1;
const PAM_PROMPT_ECHO_ON: c_int = 2;
/// Upper bound on messages in one conversation round (Linux-PAM's own).
const PAM_MAX_NUM_MSG: c_int = 32;

#[repr(C)]
struct PamMessage {
    msg_style: c_int,
    msg: *const c_char,
}

#[repr(C)]
struct PamResponse {
    resp: *mut c_char,
    resp_retcode: c_int,
}

#[repr(C)]
struct PamConv {
    conv: Option<
        unsafe extern "C" fn(c_int, *mut *const PamMessage, *mut *mut PamResponse, *mut c_void) -> c_int,
    >,
    appdata_ptr: *mut c_void,
}

#[link(name = "pam")]
extern "C" {
    fn pam_start(service: *const c_char, user: *const c_char, conv: *const PamConv, handle: *mut *mut c_void) -> c_int;
    fn pam_authenticate(handle: *mut c_void, flags: c_int) -> c_int;
    fn pam_acct_mgmt(handle: *mut c_void, flags: c_int) -> c_int;
    fn pam_end(handle: *mut c_void, status: c_int) -> c_int;
}

/// What the conversation callback answers with.
struct Credentials {
    user: CString,
    password: Zeroizing<Vec<u8>>,
}

/// Answers PAM's prompts: the password for hidden prompts, the user name
/// for visible ones, nothing for informational messages.
unsafe extern "C" fn converse(
    count: c_int,
    messages: *mut *const PamMessage,
    responses: *mut *mut PamResponse,
    data: *mut c_void,
) -> c_int {
    if count <= 0 || count > PAM_MAX_NUM_MSG || messages.is_null() || responses.is_null() || data.is_null() {
        return PAM_CONV_ERR;
    }
    // SAFETY: `data` is the `Credentials` passed to `pam_start` below; it
    // outlives the PAM transaction.
    let credentials = unsafe { &*(data as *const Credentials) };
    // PAM frees the array and each answer with free(), so both come from
    // the C allocator.
    // SAFETY: calloc with a non-zero, bounded element count.
    let array = unsafe { libc::calloc(count as usize, std::mem::size_of::<PamResponse>()) } as *mut PamResponse;
    if array.is_null() {
        return PAM_BUF_ERR;
    }
    for i in 0..count as usize {
        // SAFETY: Linux-PAM passes `count` valid message pointers.
        let message = unsafe { *messages.add(i) };
        if message.is_null() {
            continue;
        }
        // SAFETY: checked non-null above; valid for the callback's duration.
        let style = unsafe { (*message).msg_style };
        let answer: Option<&[u8]> = match style {
            PAM_PROMPT_ECHO_OFF => Some(&credentials.password),
            PAM_PROMPT_ECHO_ON => Some(credentials.user.as_bytes()),
            _ => None,
        };
        if let Some(bytes) = answer {
            // SAFETY: malloc of len + 1 bytes, filled and NUL-terminated
            // before use; PAM takes ownership.
            unsafe {
                let copy = libc::malloc(bytes.len() + 1) as *mut u8;
                if copy.is_null() {
                    for j in 0..i {
                        libc::free((*array.add(j)).resp as *mut c_void);
                    }
                    libc::free(array as *mut c_void);
                    return PAM_BUF_ERR;
                }
                ptr::copy_nonoverlapping(bytes.as_ptr(), copy, bytes.len());
                *copy.add(bytes.len()) = 0;
                (*array.add(i)).resp = copy as *mut c_char;
            }
        }
    }
    // SAFETY: `responses` is a valid out-pointer provided by PAM.
    unsafe { *responses = array };
    PAM_SUCCESS
}

/// Checks `password` for `user` against the PAM service `service`
/// (`/etc/pam.d/<service>`). Blocks for as long as PAM does (a failed
/// attempt is delayed by design), so call it from a worker thread.
pub fn authenticate(service: &str, user: &str, password: Zeroizing<String>) -> bool {
    let (Ok(service), Ok(user)) = (CString::new(service), CString::new(user)) else {
        return false;
    };
    if password.contains('\0') {
        return false;
    }
    let credentials = Credentials {
        user: user.clone(),
        password: Zeroizing::new(password.as_bytes().to_vec()),
    };
    drop(password);
    let conversation = PamConv {
        conv: Some(converse),
        appdata_ptr: &credentials as *const Credentials as *mut c_void,
    };
    let mut handle: *mut c_void = ptr::null_mut();
    // SAFETY: all pointers are valid for the duration of the transaction;
    // `credentials` and `conversation` live until after `pam_end`.
    unsafe {
        if pam_start(service.as_ptr(), user.as_ptr(), &conversation, &mut handle) != PAM_SUCCESS || handle.is_null() {
            return false;
        }
        let mut status = pam_authenticate(handle, 0);
        if status == PAM_SUCCESS {
            // An expired or locked account must not unlock the session.
            status = pam_acct_mgmt(handle, 0);
        }
        pam_end(handle, status);
        status == PAM_SUCCESS
    }
}

/// The login name and full name of the user running this process, from the
/// password database.
pub fn current_user() -> (String, String) {
    let uid = rustix::process::getuid().as_raw();
    let fallback = std::env::var("USER").unwrap_or_else(|_| "user".into());
    let Ok(passwd) = std::fs::read_to_string("/etc/passwd") else {
        return (fallback.clone(), fallback);
    };
    parse_passwd(&passwd, uid).unwrap_or((fallback.clone(), fallback))
}

/// The (login, full name) of `uid` in passwd(5) text.
pub fn parse_passwd(passwd: &str, uid: u32) -> Option<(String, String)> {
    passwd.lines().find_map(|line| {
        let mut fields = line.split(':');
        let login = fields.next()?;
        let _password = fields.next()?;
        if fields.next()?.parse::<u32>().ok()? != uid {
            return None;
        }
        let _gid = fields.next()?;
        // GECOS: "Full Name,room,phone,..."
        let full = fields.next().unwrap_or("").split(',').next().unwrap_or("").trim();
        Some((login.to_owned(), if full.is_empty() { login.to_owned() } else { full.to_owned() }))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passwd_lookup_uses_the_first_gecos_field() {
        let passwd = "root:x:0:0:root:/root:/bin/bash\nasha:x:1000:1000:Asha Rao,,,:/home/asha:/bin/bash\n\
                      svc:x:999:999::/var/lib/svc:/usr/sbin/nologin\nbroken line\n";
        assert_eq!(parse_passwd(passwd, 1000), Some(("asha".into(), "Asha Rao".into())));
        assert_eq!(parse_passwd(passwd, 999), Some(("svc".into(), "svc".into())));
        assert_eq!(parse_passwd(passwd, 4242), None);
    }

    #[test]
    fn nonsense_credentials_are_refused_without_calling_pam() {
        assert!(!authenticate("boswas\0lock", "user", Zeroizing::new("x".into())));
        assert!(!authenticate("boswas-lock", "us\0er", Zeroizing::new("x".into())));
        assert!(!authenticate("boswas-lock", "user", Zeroizing::new("a\0b".into())));
    }

    #[test]
    fn an_unknown_service_or_wrong_password_fails_closed() {
        // Without /etc/pam.d/boswas-test-missing PAM falls back to "other",
        // which denies; either way the answer must be "no".
        assert!(!authenticate(
            "boswas-test-missing",
            "boswas-no-such-user",
            Zeroizing::new("definitely wrong".into())
        ));
    }
}
