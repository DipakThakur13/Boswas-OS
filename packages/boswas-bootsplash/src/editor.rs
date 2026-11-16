//! The passphrase being typed, and the keys that edit it.
//!
//! The passphrase lives in one fixed-capacity buffer that is wiped when it is
//! cleared, taken or dropped; it is never logged, formatted or copied except
//! into the reply to the client that asked for it.

use zeroize::{Zeroize, Zeroizing};

/// Longest passphrase accepted, in bytes. Further input is ignored.
pub const MAX_PASSPHRASE: usize = 1024;

/// One editing action decoded from terminal input.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    /// One byte of the passphrase (ASCII, or part of a UTF-8 sequence).
    Byte(u8),
    Backspace,
    /// Ctrl-U or Ctrl-W: discard everything typed.
    Clear,
    Enter,
    /// The Escape key on its own.
    Escape,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    Ground,
    Escape,
    Csi,
    Ss3,
}

/// Decodes the kernel-translated byte stream of a console keyboard.
///
/// The console delivers a function or cursor key as one escape sequence in
/// one read, so a batch that ends right after ESC means the Escape key.
/// Sequences (arrows, function keys, Alt chords) never reach the passphrase.
#[derive(Debug)]
pub struct KeyDecoder {
    state: State,
}

impl Default for KeyDecoder {
    fn default() -> Self {
        Self { state: State::Ground }
    }
}

impl KeyDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Decode everything one `read()` returned.
    pub fn decode(&mut self, batch: &[u8]) -> Vec<Key> {
        let mut keys = Vec::new();
        for &byte in batch {
            match self.state {
                State::Ground => match byte {
                    0x1b => self.state = State::Escape,
                    b'\r' | b'\n' => keys.push(Key::Enter),
                    0x7f | 0x08 => keys.push(Key::Backspace),
                    0x15 | 0x17 => keys.push(Key::Clear),
                    b'\t' => keys.push(Key::Byte(byte)),
                    0x00..=0x1f => {}
                    _ => keys.push(Key::Byte(byte)),
                },
                State::Escape => match byte {
                    b'[' => self.state = State::Csi,
                    b'O' => self.state = State::Ss3,
                    0x1b => keys.push(Key::Escape),
                    // ESC followed by a key is an Alt chord: ignore both.
                    _ => self.state = State::Ground,
                },
                State::Csi => {
                    if (0x40..=0x7e).contains(&byte) {
                        self.state = State::Ground;
                    }
                }
                State::Ss3 => self.state = State::Ground,
            }
        }
        if self.state == State::Escape {
            keys.push(Key::Escape);
        }
        self.state = State::Ground;
        keys
    }
}

/// The passphrase buffer.
pub struct Passphrase {
    bytes: Zeroizing<Vec<u8>>,
}

impl std::fmt::Debug for Passphrase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Passphrase(..)")
    }
}

impl Default for Passphrase {
    fn default() -> Self {
        Self::new()
    }
}

impl Passphrase {
    pub fn new() -> Self {
        // Full capacity up front: the buffer never reallocates, so no copy of
        // a partial passphrase is left behind in freed memory.
        Self { bytes: Zeroizing::new(Vec::with_capacity(MAX_PASSPHRASE)) }
    }

    /// Apply an editing key. Returns true when the visible state changed.
    pub fn apply(&mut self, key: Key) -> bool {
        match key {
            Key::Byte(byte) => {
                if self.bytes.len() >= MAX_PASSPHRASE {
                    return false;
                }
                self.bytes.push(byte);
                true
            }
            Key::Backspace => {
                if self.bytes.is_empty() {
                    return false;
                }
                // Remove one character: UTF-8 continuation bytes, then the lead byte.
                while let Some(mut byte) = self.bytes.pop() {
                    let continuation = byte & 0xc0 == 0x80;
                    byte.zeroize();
                    if !continuation {
                        break;
                    }
                }
                true
            }
            Key::Clear => {
                let changed = !self.bytes.is_empty();
                self.clear();
                changed
            }
            Key::Enter | Key::Escape => false,
        }
    }

    /// Number of characters typed (UTF-8 sequences count once).
    pub fn chars(&self) -> usize {
        self.bytes.iter().filter(|&&b| b & 0xc0 != 0x80).count()
    }

    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    /// Wipe the buffer.
    pub fn clear(&mut self) {
        self.bytes.zeroize();
    }

    /// Hand the passphrase over and leave an empty, wiped buffer behind.
    pub fn take(&mut self) -> Zeroizing<Vec<u8>> {
        std::mem::replace(&mut self.bytes, Zeroizing::new(Vec::with_capacity(MAX_PASSPHRASE)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn typed(input: &[u8]) -> Passphrase {
        let mut decoder = KeyDecoder::new();
        let mut pass = Passphrase::new();
        for key in decoder.decode(input) {
            pass.apply(key);
        }
        pass
    }

    #[test]
    fn plain_typing_and_enter() {
        let mut decoder = KeyDecoder::new();
        assert_eq!(decoder.decode(b"ab\r"), vec![Key::Byte(b'a'), Key::Byte(b'b'), Key::Enter]);
        assert_eq!(decoder.decode(b"\n"), vec![Key::Enter]);
        let mut pass = typed(b"correct horse");
        assert_eq!(pass.chars(), 13);
        assert_eq!(&**pass.take(), b"correct horse");
        assert!(pass.is_empty());
    }

    #[test]
    fn backspace_removes_one_character() {
        let mut pass = typed(b"abc\x7f");
        assert_eq!(&**pass.take(), b"ab");
        let mut pass = typed(b"abc\x08\x08\x08\x08");
        assert!(pass.is_empty());
        assert!(!pass.apply(Key::Backspace), "backspace on an empty field changes nothing");
        // A two-byte and a four-byte character are each removed whole.
        let mut pass = typed("a\u{e9}\u{1f512}".as_bytes());
        assert_eq!(pass.chars(), 3);
        pass.apply(Key::Backspace);
        assert_eq!(pass.chars(), 2);
        pass.apply(Key::Backspace);
        assert_eq!(&**pass.take(), b"a");
    }

    #[test]
    fn clear_keys() {
        assert!(typed(b"secret\x15").is_empty());
        assert!(typed(b"secret\x17").is_empty());
        let mut pass = typed(b"secret\x15new");
        assert_eq!(&**pass.take(), b"new");
    }

    #[test]
    fn escape_alone_versus_sequences() {
        let mut decoder = KeyDecoder::new();
        assert_eq!(decoder.decode(b"\x1b"), vec![Key::Escape]);
        // Arrow keys, F5, Home (SS3) and an Alt chord produce nothing.
        assert_eq!(decoder.decode(b"\x1b[A"), vec![]);
        assert_eq!(decoder.decode(b"\x1b[15~"), vec![]);
        assert_eq!(decoder.decode(b"\x1bOH"), vec![]);
        assert_eq!(decoder.decode(b"\x1bx"), vec![]);
        // Sequences in the middle of typing do not leak bytes into the passphrase.
        assert_eq!(decoder.decode(b"a\x1b[Cb"), vec![Key::Byte(b'a'), Key::Byte(b'b')]);
        // An unfinished sequence does not swallow the next batch.
        assert_eq!(decoder.decode(b"\x1b[1"), vec![]);
        assert_eq!(decoder.decode(b"z"), vec![Key::Byte(b'z')]);
        assert_eq!(decoder.decode(b"\x1b\x1b"), vec![Key::Escape, Key::Escape]);
    }

    #[test]
    fn control_characters_are_ignored_but_tab_is_kept() {
        let mut decoder = KeyDecoder::new();
        assert_eq!(decoder.decode(&[0x00, 0x01, 0x03, 0x04, 0x1a, 0x1f]), vec![]);
        assert_eq!(decoder.decode(b"\t"), vec![Key::Byte(b'\t')]);
    }

    #[test]
    fn length_is_bounded_and_the_buffer_never_grows() {
        let mut pass = Passphrase::new();
        let capacity = pass.bytes.capacity();
        for _ in 0..MAX_PASSPHRASE {
            assert!(pass.apply(Key::Byte(b'x')));
        }
        assert!(!pass.apply(Key::Byte(b'x')));
        assert_eq!(pass.chars(), MAX_PASSPHRASE);
        assert_eq!(pass.bytes.capacity(), capacity);
        let taken = pass.take();
        assert_eq!(taken.len(), MAX_PASSPHRASE);
        assert_eq!(pass.bytes.capacity(), capacity);
    }

    #[test]
    fn the_passphrase_is_never_printed() {
        let pass = typed(b"hunter2");
        assert_eq!(format!("{pass:?}"), "Passphrase(..)");
    }

    #[test]
    fn random_input_never_panics() {
        // xorshift: deterministic pseudo-random batches.
        let mut seed = 0x9e37_79b9_7f4a_7c15u64;
        let mut decoder = KeyDecoder::new();
        let mut pass = Passphrase::new();
        for _ in 0..2000 {
            let mut batch = [0u8; 37];
            for b in batch.iter_mut() {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                *b = seed as u8;
            }
            let len = (seed % 37) as usize;
            for key in decoder.decode(&batch[..len]) {
                pass.apply(key);
            }
            assert!(pass.chars() <= MAX_PASSPHRASE);
        }
    }
}
