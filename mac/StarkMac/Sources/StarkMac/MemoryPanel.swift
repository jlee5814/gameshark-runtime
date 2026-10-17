import StarkKit
import SwiftUI

/// Saved memories inside the chat card: the runtime's list, an Add field, and per row
/// Edit and Forget. Every change is a typed command; nothing here stores a fact locally.
struct MemoryPanel: View {
    @Bindable var model: StarkModel
    @State private var adding = ""
    @State private var editing: String?
    @State private var editText = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("Saved memories", systemImage: "brain").font(.subheadline.weight(.semibold))
                Spacer()
                Button("Done") { model.showMemories = false }.controlSize(.small)
            }
            if !model.state.memoryInject {
                Text("Memory is off: saved facts do not reach the model.")
                    .font(.caption).foregroundStyle(.orange)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    if model.state.memories.isEmpty && model.state.memoryOps.isEmpty {
                        Text("Nothing saved. Type /remember followed by a fact, or add one below.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(model.state.memories) { m in row(m) }
                    ForEach(model.state.memoryOps) { op in pending(op) }
                }
            }
            if let notice = model.state.memoryNotice {
                Text(notice).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                TextField("Add a fact for Stark to keep", text: $adding)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(add)
                    .disabled(!model.state.canWriteMemory)
                Button("Save", action: add)
                    .disabled(!model.state.canWriteMemory || adding.isEmpty)
            }
            Text("Forgetting removes a fact from saved memory. Earlier chat that mentions it "
                 + "stays in the conversation.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private func add() {
        model.addMemory(adding)
        adding = ""
    }

    @ViewBuilder
    private func row(_ m: MemoryItem) -> some View {
        HStack(alignment: .top, spacing: 6) {
            if editing == m.id {
                TextField("Memory", text: $editText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { commit(m) }
                Button("Save") { commit(m) }.controlSize(.small)
                Button("Cancel") { editing = nil }.controlSize(.small)
            } else {
                Text(m.content).font(.callout).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button {
                    editing = m.id
                    editText = m.content
                } label: { Image(systemName: "pencil") }
                    .buttonStyle(.borderless)
                    .disabled(!model.state.canWriteMemory)
                    .help("Correct this memory")
                Button(role: .destructive) { model.forgetMemory(m.id) } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .disabled(!model.state.canForgetMemory)
                .help("Forget this memory")
            }
        }
        .padding(6)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private func pending(_ op: MemoryOp) -> some View {
        if let text = op.text {
            HStack {
                Text(text).font(.callout).foregroundStyle(.secondary)
                Spacer()
                switch op.status {
                case .pending: ProgressView().controlSize(.mini)
                case .rejected:
                    Button("Dismiss") { model.dismissMemoryOp(op.id) }.controlSize(.small)
                }
            }
            .padding(6)
        }
    }

    private func commit(_ m: MemoryItem) {
        model.correctMemory(m, text: editText)
        editing = nil
    }
}
