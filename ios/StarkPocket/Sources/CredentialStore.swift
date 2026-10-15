import Foundation
import Security
import StarkKit

/// The only durable secrets on the phone: the pairing record and the device key, in the
/// Keychain, bound to this device, readable after first unlock. Nothing here is agent
/// state; conversation rows are rebuilt from the runtime every session.
struct CredentialStore {
    enum Item: String, CaseIterable {
        case credential, enclaveKey, softwareKey
    }

    var service = "dev.gameshark.starkpocket"

    func save(_ data: Data, as item: Item) throws {
        delete(item)
        let attrs: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: item.rawValue,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data,
        ]
        let status = SecItemAdd(attrs as CFDictionary, nil)
        guard status == errSecSuccess else { throw StoreError.keychain(status) }
    }

    func load(_ item: Item) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: item.rawValue,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }

    func delete(_ item: Item) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: item.rawValue,
        ]
        SecItemDelete(query as CFDictionary)
    }

    func wipe() {
        Item.allCases.forEach(delete)
    }

    enum StoreError: Error, Equatable {
        case keychain(OSStatus)
    }

    // MARK: device

    /// A fresh device key: in the Secure Enclave when the hardware has one (a real
    /// iPhone), otherwise in software (the simulator).
    static func newSigner() throws -> (any DeviceSigner, Item, Data) {
        if EnclaveSigner.isAvailable {
            let k = try EnclaveSigner()
            return (k, .enclaveKey, k.dataRepresentation)
        }
        let k = SoftwareSigner()
        return (k, .softwareKey, k.rawRepresentation)
    }

    func saveDevice(_ credential: DeviceCredential, keyItem: Item, keyData: Data) throws {
        try save(keyData, as: keyItem)
        try save(JSONEncoder().encode(credential), as: .credential)
    }

    func loadDevice() -> DeviceEndpoint? {
        guard let raw = load(.credential),
              let cred = try? JSONDecoder().decode(DeviceCredential.self, from: raw)
        else { return nil }
        if let blob = load(.enclaveKey), let k = try? EnclaveSigner(dataRepresentation: blob) {
            return DeviceEndpoint(credential: cred, signer: k)
        }
        if let blob = load(.softwareKey), let k = try? SoftwareSigner(rawRepresentation: blob) {
            return DeviceEndpoint(credential: cred, signer: k)
        }
        return nil
    }
}
